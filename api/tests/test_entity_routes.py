"""Route tests for Entity + setup endpoints (Contract F11).

FastAPI TestClient with the service layer mocked, following the existing
route-test pattern (dependency overrides for get_current_user / require_role).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.index import app
from src.models.entity import Entity
from src.models.user import User
from src.services.coa_service import CoaServiceError, TemplateNotFoundError
from src.services.entity_service import (
    DuplicateEntityNameError,
    DuplicateLedgerNameError,
    EntityNotFoundError,
    EntityServiceError,
)
from src.services.setup_service import SetupError
from src.validators.entity import EntityStatus, SetupResult

ENTITY_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


def make_entity(**overrides) -> Entity:
    defaults = {
        "id": ENTITY_ID,
        "name": "Acme Holdings Ltd",
        "legal_type": "limited_company",
        "framework": "uk-gaap",
        "fiscal_year_start": "04-06",
        "vat_registered": True,
        "vat_number": "GB123456789",
        "vat_scheme": "standard",
        "currency": "GBP",
        "ledger_name": "entity-acme123",
        "created_by": USER_ID,
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    defaults.update(overrides)
    return Entity(**defaults)


def make_user(role: str) -> User:
    return User(
        id=USER_ID,
        email="owner@acme.test",
        hashed_password="h",
        display_name="Owner",
        role=role,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


def make_setup_result(**overrides) -> SetupResult:
    defaults = {
        "entity_id": ENTITY_ID,
        "coa_count": 65,
        "bank_account_ids": [uuid.uuid4()],
        "ledger_name": "entity-acme123",
        "opening_transaction_ids": [uuid.uuid4()],
        "opening_draft_total_pence": 1000000,
    }
    defaults.update(overrides)
    return SetupResult(**defaults)


def make_status(**overrides) -> EntityStatus:
    defaults = {
        "entity": {
            "id": str(ENTITY_ID),
            "name": "Acme Holdings Ltd",
            "legal_type": "limited_company",
            "framework": "uk-gaap",
            "fiscal_year_start": "04-06",
            "vat_registered": True,
            "vat_number": "GB123456789",
            "vat_scheme": "standard",
            "currency": "GBP",
            "ledger_name": "entity-acme123",
            "created_by": str(USER_ID),
            "is_active": True,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        },
        "has_coa": True,
        "account_count": 65,
        "bank_accounts": [],
        "pending_opening_transactions": [],
        "ledger_name": "entity-acme123",
        "formance": "reachable",
        "parity": "ok",
    }
    defaults.update(overrides)
    return EntityStatus(**defaults)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_overrides() -> None:
    yield
    app.dependency_overrides.clear()


def _override_auth(user: User) -> None:
    from src.middleware.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: user


# ---------------------------------------------------------------------------
# POST / — create entity
# ---------------------------------------------------------------------------

def test_create_entity_requires_auth(client: TestClient) -> None:
    """Missing token -> 401."""
    resp = client.post("/api/v1/entities", json={"name": "Acme"})
    assert resp.status_code == 401


def test_create_entity_success(client: TestClient) -> None:
    """Auth + valid body -> 201 with entity, no setup."""
    _override_auth(make_user("owner"))
    with patch(
        "src.routes.entities.EntityService.create_entity",
        new_callable=AsyncMock,
    ) as mock_create:
        mock_create.return_value = make_entity()
        resp = client.post(
            "/api/v1/entities",
            json={"name": "Acme Holdings Ltd", "legal_type": "limited_company"},
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["entity"]["name"] == "Acme Holdings Ltd"
    assert data["entity"]["ledger_name"] == "entity-acme123"
    assert data["setup"] is None

    created_by = mock_create.await_args.kwargs["created_by"]
    assert created_by == USER_ID


def test_create_entity_with_setup_runs_setup(client: TestClient) -> None:
    """A `setup` body runs setup_entity immediately; 201 includes the result."""
    _override_auth(make_user("owner"))
    with patch(
        "src.routes.entities.EntityService.create_entity",
        new_callable=AsyncMock,
    ) as mock_create, patch(
        "src.routes.entities.setup_entity",
        new_callable=AsyncMock,
    ) as mock_setup:
        mock_create.return_value = make_entity()
        mock_setup.return_value = make_setup_result()
        resp = client.post(
            "/api/v1/entities",
            json={
                "name": "Acme Holdings Ltd",
                "setup": {
                    "template_name": "uk_limited_company_vat",
                    "bank_accounts": [
                        {"name": "Barclays", "opening_balance": 1000}
                    ],
                    "opening_balances": {
                        "opening_date": "2026-04-06",
                        "equity_account_code": "3000",
                        "assets": [{"code": "1000", "balance_pence": 1000000}],
                    },
                },
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["setup"]["coa_count"] == 65
    assert data["setup"]["opening_draft_total_pence"] == 1000000
    assert mock_setup.await_count == 1
    assert mock_setup.await_args.args[2] == "uk_limited_company_vat"


def test_create_with_setup_template_not_found_returns_404(
    client: TestClient,
) -> None:
    """F12 regression: a setup failure inside POST / must surface the mapped
    404 {type:'coa_error'} — never a 500 pydantic ValidationError."""
    _override_auth(make_user("owner"))
    with patch(
        "src.routes.entities.EntityService.create_entity",
        new_callable=AsyncMock,
    ) as mock_create, patch(
        "src.routes.entities.setup_entity",
        new_callable=AsyncMock,
    ) as mock_setup:
        mock_create.return_value = make_entity()
        mock_setup.side_effect = TemplateNotFoundError("nonexistent")
        resp = client.post(
            "/api/v1/entities",
            json={
                "name": "Acme Holdings Ltd",
                "setup": {"template_name": "nonexistent"},
            },
        )

    assert resp.status_code == 404
    body = resp.json()
    assert body["type"] == "coa_error"
    assert "nonexistent" in body["message"]
    # The entity row was committed before setup ran — it remains created.
    assert mock_create.await_count == 1


def test_create_with_setup_setup_error_returns_409(client: TestClient) -> None:
    """F12 regression: a SetupError inside POST / maps to 409
    {type:'setup_error'} instead of a 500 ValidationError."""
    _override_auth(make_user("owner"))
    with patch(
        "src.routes.entities.EntityService.create_entity",
        new_callable=AsyncMock,
    ) as mock_create, patch(
        "src.routes.entities.setup_entity",
        new_callable=AsyncMock,
    ) as mock_setup:
        mock_create.return_value = make_entity()
        mock_setup.side_effect = SetupError("already set up")
        resp = client.post(
            "/api/v1/entities",
            json={
                "name": "Acme Holdings Ltd",
                "setup": {"template_name": "uk_limited_company_vat"},
            },
        )

    assert resp.status_code == 409
    body = resp.json()
    assert body["type"] == "setup_error"
    assert "already set up" in body["message"]
    assert mock_create.await_count == 1


def test_create_with_setup_success_still_201(client: TestClient) -> None:
    """F12 regression guard: a successful setup still embeds the SetupResult
    in the 201 EntitySetupResponse."""
    _override_auth(make_user("owner"))
    with patch(
        "src.routes.entities.EntityService.create_entity",
        new_callable=AsyncMock,
    ) as mock_create, patch(
        "src.routes.entities.setup_entity",
        new_callable=AsyncMock,
    ) as mock_setup:
        mock_create.return_value = make_entity()
        mock_setup.return_value = make_setup_result()
        resp = client.post(
            "/api/v1/entities",
            json={
                "name": "Acme Holdings Ltd",
                "setup": {"template_name": "uk_limited_company_vat"},
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["entity"]["name"] == "Acme Holdings Ltd"
    assert body["setup"]["entity_id"] == str(ENTITY_ID)
    assert body["setup"]["coa_count"] == 65
    assert mock_setup.await_count == 1


def test_create_entity_duplicate_name_409(client: TestClient) -> None:
    """Duplicate entity name -> 409."""
    _override_auth(make_user("owner"))
    with patch(
        "src.routes.entities.EntityService.create_entity",
        new_callable=AsyncMock,
    ) as mock_create:
        mock_create.side_effect = DuplicateEntityNameError("Acme Holdings Ltd")
        resp = client.post("/api/v1/entities", json={"name": "Acme Holdings Ltd"})

    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# GET / and GET /{id}
# ---------------------------------------------------------------------------

def test_list_entities(client: TestClient) -> None:
    """Contract F23: the list is PUBLIC — no token, no auth override."""
    with patch(
        "src.routes.entities.EntityService.list_entities",
        new_callable=AsyncMock,
    ) as mock_list:
        mock_list.return_value = [make_entity()]
        resp = client.get("/api/v1/entities")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == str(ENTITY_ID)


def test_list_entities_no_token_required(client: TestClient) -> None:
    """F23 regression: a tokenless GET /entities is 200 (local-trust),
    never a 401 from the dropped get_current_user dependency."""
    with patch(
        "src.routes.entities.EntityService.list_entities",
        new_callable=AsyncMock,
    ) as mock_list:
        mock_list.return_value = [make_entity()]
        resp = client.get("/api/v1/entities")

    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "Acme Holdings Ltd"


def test_get_entity(client: TestClient) -> None:
    _override_auth(make_user("viewer"))
    with patch(
        "src.routes.entities.EntityService.get_entity",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.return_value = make_entity()
        resp = client.get(f"/api/v1/entities/{ENTITY_ID}")

    assert resp.status_code == 200
    assert resp.json()["name"] == "Acme Holdings Ltd"


def test_get_entity_404(client: TestClient) -> None:
    _override_auth(make_user("viewer"))
    with patch(
        "src.routes.entities.EntityService.get_entity",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.return_value = None
        resp = client.get(f"/api/v1/entities/{ENTITY_ID}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /{entity_id} — update entity details (Contract F27)
# ---------------------------------------------------------------------------

def test_update_entity_requires_admin_role(client: TestClient) -> None:
    """viewer role -> 403 on PATCH (role guard)."""
    _override_auth(make_user("viewer"))
    resp = client.patch(
        f"/api/v1/entities/{ENTITY_ID}", json={"name": "Renamed Ltd"}
    )
    assert resp.status_code == 403


def test_update_entity_success(client: TestClient) -> None:
    """owner role -> 200 with the updated EntityResponse."""
    _override_auth(make_user("owner"))
    with patch(
        "src.routes.entities.EntityService.update_entity",
        new_callable=AsyncMock,
    ) as mock_update:
        mock_update.return_value = make_entity(name="Acme Renamed Ltd")
        resp = client.patch(
            f"/api/v1/entities/{ENTITY_ID}",
            json={"name": "Acme Renamed Ltd", "vat_registered": False},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(ENTITY_ID)
    assert data["name"] == "Acme Renamed Ltd"
    mock_update.assert_awaited_once()
    args = mock_update.await_args.args
    assert args[1] == ENTITY_ID
    assert args[2].name == "Acme Renamed Ltd"
    assert args[2].vat_registered is False


def test_update_entity_not_found_404(client: TestClient) -> None:
    """F27: unknown entity -> 404 with the EntityNotFoundError detail."""
    _override_auth(make_user("admin"))
    with patch(
        "src.routes.entities.EntityService.update_entity",
        new_callable=AsyncMock,
    ) as mock_update:
        mock_update.side_effect = EntityNotFoundError(ENTITY_ID)
        resp = client.patch(
            f"/api/v1/entities/{ENTITY_ID}", json={"name": "Renamed Ltd"}
        )

    assert resp.status_code == 404
    body = resp.json()
    assert "not found" in body["detail"]


def test_update_entity_duplicate_name_409(client: TestClient) -> None:
    """F27: a duplicate name surfaces as a 409."""
    _override_auth(make_user("owner"))
    with patch(
        "src.routes.entities.EntityService.update_entity",
        new_callable=AsyncMock,
    ) as mock_update:
        mock_update.side_effect = DuplicateEntityNameError("Taken Ltd")
        resp = client.patch(
            f"/api/v1/entities/{ENTITY_ID}", json={"name": "Taken Ltd"}
        )

    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /{id}/setup
# ---------------------------------------------------------------------------

def test_setup_requires_admin_role(client: TestClient) -> None:
    """viewer role -> 403 on the setup endpoint (role guard)."""
    _override_auth(make_user("viewer"))
    resp = client.post(
        f"/api/v1/entities/{ENTITY_ID}/setup",
        json={"template_name": "uk_limited_company_vat"},
    )
    assert resp.status_code == 403


def test_setup_success_owner(client: TestClient) -> None:
    """owner role -> 200 with the SetupResult."""
    _override_auth(make_user("owner"))
    with patch(
        "src.routes.entities.setup_entity",
        new_callable=AsyncMock,
    ) as mock_setup:
        mock_setup.return_value = make_setup_result()
        resp = client.post(
            f"/api/v1/entities/{ENTITY_ID}/setup",
            json={"template_name": "uk_limited_company_vat"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["entity_id"] == str(ENTITY_ID)
    assert data["coa_count"] == 65
    assert data["ledger_name"] == "entity-acme123"


def test_setup_already_set_up_409(client: TestClient) -> None:
    _override_auth(make_user("admin"))
    with patch(
        "src.routes.entities.setup_entity",
        new_callable=AsyncMock,
    ) as mock_setup:
        mock_setup.side_effect = SetupError("already set up")
        resp = client.post(
            f"/api/v1/entities/{ENTITY_ID}/setup",
            json={"template_name": "uk_limited_company_vat"},
        )

    assert resp.status_code == 409


def test_setup_unknown_entity_404(client: TestClient) -> None:
    _override_auth(make_user("owner"))
    with patch(
        "src.routes.entities.setup_entity",
        new_callable=AsyncMock,
    ) as mock_setup:
        mock_setup.side_effect = EntityNotFoundError(ENTITY_ID)
        resp = client.post(
            f"/api/v1/entities/{ENTITY_ID}/setup",
            json={"template_name": "uk_limited_company_vat"},
        )

    assert resp.status_code == 404


def test_setup_template_not_found_404_coa_error(client: TestClient) -> None:
    """F12: a missing COA template is a 404 {type:'coa_error'} — never a 500."""
    _override_auth(make_user("owner"))
    with patch(
        "src.routes.entities.setup_entity",
        new_callable=AsyncMock,
    ) as mock_setup:
        mock_setup.side_effect = TemplateNotFoundError("uk_nope")
        resp = client.post(
            f"/api/v1/entities/{ENTITY_ID}/setup",
            json={"template_name": "uk_nope"},
        )

    assert resp.status_code == 404
    body = resp.json()
    assert body["type"] == "coa_error"
    assert "uk_nope" in body["message"]


def test_setup_coa_service_error_404_coa_error(client: TestClient) -> None:
    """Any CoaServiceError maps to 404 {type:'coa_error'}."""
    _override_auth(make_user("owner"))
    with patch(
        "src.routes.entities.setup_entity",
        new_callable=AsyncMock,
    ) as mock_setup:
        mock_setup.side_effect = CoaServiceError("COA exploded", status_code=400)
        resp = client.post(
            f"/api/v1/entities/{ENTITY_ID}/setup",
            json={"template_name": "uk_limited_company_vat"},
        )

    assert resp.status_code == 404
    body = resp.json()
    assert body["type"] == "coa_error"
    assert body["message"] == "COA exploded"


def test_create_entity_duplicate_ledger_name_409(client: TestClient) -> None:
    """F12: a duplicate ledger_name is a 409 {type:'duplicate_ledger_name'}."""
    _override_auth(make_user("owner"))
    with patch(
        "src.routes.entities.EntityService.create_entity",
        new_callable=AsyncMock,
    ) as mock_create:
        mock_create.side_effect = DuplicateLedgerNameError("entity-abc123")
        resp = client.post("/api/v1/entities", json={"name": "Acme Holdings Ltd"})

    assert resp.status_code == 409
    body = resp.json()
    assert body["type"] == "duplicate_ledger_name"
    assert "entity-abc123" in body["message"]


def test_setup_already_set_up_409_body_shape(client: TestClient) -> None:
    """F12: the already-set-up conflict is a 409 {type:'setup_error'}."""
    _override_auth(make_user("owner"))
    with patch(
        "src.routes.entities.setup_entity",
        new_callable=AsyncMock,
    ) as mock_setup:
        mock_setup.side_effect = SetupError("Entity already set up")
        resp = client.post(
            f"/api/v1/entities/{ENTITY_ID}/setup",
            json={"template_name": "uk_limited_company_vat"},
        )

    assert resp.status_code == 409
    body = resp.json()
    assert body["type"] == "setup_error"
    assert "already set up" in body["message"]


# ---------------------------------------------------------------------------
# GET /{id}/status
# ---------------------------------------------------------------------------

def test_entity_status(client: TestClient) -> None:
    _override_auth(make_user("viewer"))
    with patch(
        "src.routes.entities.get_entity_status",
        new_callable=AsyncMock,
    ) as mock_status:
        mock_status.return_value = make_status()
        resp = client.get(f"/api/v1/entities/{ENTITY_ID}/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_coa"] is True
    assert data["account_count"] == 65
    assert data["formance"] == "reachable"
    assert data["parity"] == "ok"


def test_entity_status_404(client: TestClient) -> None:
    _override_auth(make_user("viewer"))
    with patch(
        "src.routes.entities.get_entity_status",
        new_callable=AsyncMock,
    ) as mock_status:
        mock_status.side_effect = EntityNotFoundError(ENTITY_ID)
        resp = client.get(f"/api/v1/entities/{ENTITY_ID}/status")

    assert resp.status_code == 404


def test_list_entities_filters_inactive(client: TestClient) -> None:
    """GET /entities excludes inactive entities by default (public route)."""
    with patch(
        "src.routes.entities.EntityService.list_entities",
        new_callable=AsyncMock,
    ) as mock_list:
        mock_list.return_value = []
        response = client.get("/api/v1/entities")
        assert response.status_code == 200
        mock_list.assert_called_once()
        assert mock_list.call_args.kwargs.get("include_inactive") is False
