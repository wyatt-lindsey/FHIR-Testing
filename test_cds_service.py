"""Tests for cds_service.py. Run: python3 -m pytest -q test_cds_service.py

These use hand-built FHIR resources, so they exercise the real
guideline.build_patient_data() and evaluate() from Phase 3. No server needed.
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import cds_service

client = TestClient(cds_service.app)
PID = "p1"
URL = f"/cds-services/{cds_service.SERVICE_ID}"


def days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def bundle(*resources, next_url=None):
    b = {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": r} for r in resources]}
    if next_url:
        b["link"] = [{"relation": "next", "url": next_url}]
    return b


def condition(snomed: str, display: str) -> dict:
    return {
        "resourceType": "Condition",
        "id": f"c-{snomed}",
        "subject": {"reference": f"Patient/{PID}"},
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
        "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": "confirmed"}]},
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": snomed, "display": display}]},
        "onsetDateTime": days_ago(2000),
    }


def egfr(value: float, ago: int) -> dict:
    return {
        "resourceType": "Observation",
        "id": f"egfr-{ago}",
        "status": "final",
        "subject": {"reference": f"Patient/{PID}"},
        "code": {"coding": [{"system": "http://loinc.org", "code": "33914-3"}]},
        "effectiveDateTime": days_ago(ago),
        "valueQuantity": {"value": value, "unit": "mL/min/{1.73_m2}"},
    }


PATIENT = {"resourceType": "Patient", "id": PID, "birthDate": "1960-01-01", "name": [{"given": ["Pat"], "family": "Test"}]}
T2D = condition("44054006", "Diabetes mellitus type 2")
CKD3 = condition("433144002", "Chronic kidney disease stage 3")


def request_body(prefetch=None, fhir_server=None):
    body = {
        "hook": "patient-view",
        "hookInstance": "test-instance",
        "context": {"userId": "Practitioner/1", "patientId": PID},
    }
    if prefetch is not None:
        body["prefetch"] = prefetch
    if fhir_server:
        body["fhirServer"] = fhir_server
    return body


def prefetch(conditions=(), egfrs=(), meds=()):
    return {
        "patient": PATIENT,
        "conditions": bundle(*conditions),
        "egfr": bundle(*egfrs),
        "uacr": bundle(),
        "meds": bundle(*meds),
    }


# Tests where an exception inside the service is the expected path. Everywhere
# else, an exception is a bug: C7 would turn it into a silent "no card", and a
# "should be silent" test would pass for the wrong reason.
ERRORS_EXPECTED = {"test_unreachable_fhir_server_is_silent_not_error"}


@pytest.fixture(autouse=True)
def no_hidden_errors(request, caplog):
    yield
    if request.node.originalname not in ERRORS_EXPECTED:
        failed = [r.getMessage() for r in caplog.records if "evaluation failed" in r.getMessage()]
        assert not failed, f"service raised and C7 hid it: {failed}"


def sglt2_order_by_reference():
    """An active canagliflozin order that names its drug via a Medication resource."""
    med = {
        "resourceType": "Medication",
        "id": "m1",
        "code": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                             "code": "1373463", "display": "canagliflozin 100 MG Oral Tablet"}]},
    }
    mr = {
        "resourceType": "MedicationRequest",
        "id": "mr1",
        "status": "active",
        "subject": {"reference": f"Patient/{PID}"},
        "medicationReference": {"reference": "Medication/m1"},
    }
    return mr, med


# ---------------------------------------------------------------- discovery


def test_discovery_lists_service_with_prefetch():
    services = client.get("/cds-services").json()["services"]
    assert len(services) == 1
    s = services[0]
    assert s["id"] == cds_service.SERVICE_ID and s["hook"] == "patient-view"
    assert set(s["prefetch"]) == {"patient", "conditions", "egfr", "uacr", "meds"}
    assert "33914-3" in s["prefetch"]["egfr"]


# ---------------------------------------------------------------- cards


def test_recommend_returns_one_valid_card():
    r = client.post(URL, json=request_body(prefetch([T2D, CKD3], [egfr(41, 30)])))
    cards = r.json()["cards"]
    assert r.status_code == 200 and len(cards) == 1
    card = cards[0]
    assert len(card["summary"]) < 140
    assert card["indicator"] in {"info", "warning", "critical"}
    assert card["source"]["label"]
    assert "SGLT2" in card["summary"]
    assert "suggestions" not in card  # C6: no orders until clinician sign-off


def test_not_applicable_is_silent():
    r = client.post(URL, json=request_body(prefetch([T2D], [egfr(95, 30)])))
    assert r.json() == {"cards": []}


def test_low_egfr_is_silent():
    r = client.post(URL, json=request_body(prefetch([T2D, CKD3], [egfr(15, 30)])))
    assert r.json() == {"cards": []}


def test_insufficient_data_card_when_no_recent_egfr():
    r = client.post(URL, json=request_body(prefetch([T2D, CKD3], [egfr(41, 800)])))
    cards = r.json()["cards"]
    assert len(cards) == 1 and "unclear" in cards[0]["summary"]
    assert len(cards[0]["summary"]) < 140


def test_sglt2_via_medication_reference_is_silent():
    mr, med = sglt2_order_by_reference()
    r = client.post(URL, json=request_body(prefetch([T2D, CKD3], [egfr(41, 30)], [mr, med])))
    assert r.json() == {"cards": []}


def test_to_patient_data_sorts_resources():
    mr, med = sglt2_order_by_reference()
    outcome = {"resourceType": "OperationOutcome", "issue": []}
    p = cds_service.to_patient_data(PATIENT, [T2D, CKD3, egfr(41, 30), mr, med, outcome])
    assert p.id == PID and len(p.conditions) == 2 and len(p.egfr) == 1
    assert p.active_meds and p.active_meds[0][1] == "canagliflozin 100 MG Oral Tablet"


# ---------------------------------------------------------------- data in


def test_missing_prefetch_falls_back_to_fhir_server(monkeypatch):
    calls = []

    def fake_fetch(base, patient_id, token):
        calls.append((base, patient_id, token))
        return PATIENT, [T2D, CKD3, egfr(41, 30)]

    monkeypatch.setattr(cds_service, "from_fhir_server", fake_fetch)
    r = client.post(URL, json=request_body(fhir_server="http://ehr.example/fhir"))
    assert calls == [("http://ehr.example/fhir", PID, None)]
    assert len(r.json()["cards"]) == 1


def test_paged_prefetch_counts_as_incomplete():
    pf = prefetch([T2D, CKD3], [egfr(41, 30)])
    pf["egfr"] = bundle(egfr(41, 30), next_url="http://ehr.example/fhir?page=2")
    assert cds_service.from_prefetch(pf) is None


def test_no_prefetch_no_server_is_silent_not_error():
    r = client.post(URL, json=request_body())
    assert r.status_code == 200 and r.json() == {"cards": []}


def test_unreachable_fhir_server_is_silent_not_error():
    r = client.post(URL, json=request_body(fhir_server="http://127.0.0.1:9/fhir"))
    assert r.status_code == 200 and r.json() == {"cards": []}


# ---------------------------------------------------------------- bad requests


def test_unknown_service_404():
    assert client.post("/cds-services/nope", json=request_body()).status_code == 404


@pytest.mark.parametrize("hook", ["order-sign", None])
def test_wrong_hook_400(hook):
    body = request_body(prefetch([T2D]))
    body["hook"] = hook
    assert client.post(URL, json=body).status_code == 400
