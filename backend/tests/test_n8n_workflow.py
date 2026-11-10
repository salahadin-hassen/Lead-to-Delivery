"""The n8n CRM deal-won demo workflow (n8n/crm-deal-won-demo.json).

Static checks on the exported JSON (structure, retention settings, no secrets),
plus the workflow's own Code-node JavaScript executed with Node.js through
tests/n8n_code_harness.mjs and chained, over real HTTP, through the sanitizing
relay (app/demo_relay.py) to the real API on ltd_test. The Crypto node is
reproduced with Node's createHmac, exactly as n8n's Crypto node computes it.
"""

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.schemas.crm import DealWonEvent
from app.security import webhook_signature
from app import demo_relay
from tests.crm_helpers import INTAKE_TABLES, SECRET, counts
from tests.relay_helpers import FakeUpstream, assert_safe_relay_response, live_api, post_raw, running_relay

REPO = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO / "n8n" / "crm-deal-won-demo.json"
HARNESS = Path(__file__).with_name("n8n_code_harness.mjs")
EXAMPLES = {
    "generic": REPO / "docs/examples/crm/generic.json",
    "gohighlevel": REPO / "docs/examples/crm/gohighlevel-style.json",
    "hubspot": REPO / "docs/examples/crm/hubspot-style.json",
}
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is required to run the workflow's Code nodes")

DEMO_ID = "0f6c2a9e-4b1d-4c8e-9a51-3e7d2b8f1c04"
OTHER_DEMO_ID = "7d3e9b12-55aa-4f0e-8c2d-91b6a4e0f7d3"
RELAY_ORIGIN = "http://127.0.0.1:8011"
RELAY_ENDPOINT = RELAY_ORIGIN + demo_relay.PATH  # the only URL either HTTP node may call
HTTP_NODES = ("Send Deal-Won Event", "Replay Deal-Won Event")


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return json.loads(WORKFLOW_PATH.read_text())


@pytest.fixture(scope="module")
def nodes(workflow) -> dict[str, dict[str, Any]]:
    return {n["name"]: n for n in workflow["nodes"]}


def code_of(nodes, name: str) -> str:
    return nodes[name]["parameters"]["jsCode"]


def params_of(nodes) -> dict[str, dict]:
    """Every node's stored parameters, as n8n's $(name).params returns them."""
    return {name: node["parameters"] for name, node in nodes.items()}


def run_code(code: str, *, input: list[dict] | None = None, nodes: dict[str, list[dict]] | None = None,
             params: dict[str, dict] | None = None):
    proc = subprocess.run([NODE, str(HARNESS)], input=json.dumps({"code": code, "input": input or [],
                          "nodes": nodes or {}, "params": params or {}}),
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(proc.stdout)


def ok(result) -> list[dict]:
    assert result["ok"], result.get("error")
    return result["items"]


def node_hmac(secret: str, value: str) -> str:
    """What n8n's Crypto node (action hmac, SHA256, hex) outputs: createHmac(type, secret).update(value)."""
    script = ("let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{const {s,v}=JSON.parse(d);"
              "process.stdout.write(require('crypto').createHmac('SHA256',s).update(v).digest('hex'))})")
    return subprocess.run([NODE, "-e", script], input=json.dumps({"s": secret, "v": value}),
                          capture_output=True, text=True, timeout=30, check=True).stdout


# ---------------------------------------------------------------- the export
def test_workflow_structure_and_connections(workflow, nodes) -> None:
    assert len(nodes) == len(workflow["nodes"]), "node names must be unique"
    allowed = {"n8n-nodes-base.manualTrigger": {1}, "n8n-nodes-base.set": {3.4}, "n8n-nodes-base.code": {2},
               "n8n-nodes-base.crypto": {2}, "n8n-nodes-base.httpRequest": {4.2},
               "n8n-nodes-base.stickyNote": {1}}
    for node in workflow["nodes"]:
        assert node["typeVersion"] in allowed[node["type"]], node["name"]
    for source, outputs in workflow["connections"].items():
        assert source in nodes
        for branch in outputs["main"]:
            for target in branch:
                assert target["node"] in nodes

    chain, current = [], "Run Demo"
    while current:
        chain.append(current)
        branches = workflow["connections"].get(current, {}).get("main", [])
        current = branches[0][0]["node"] if branches else None
    assert chain == ["Run Demo", "Select CRM Example", "Check Relay Endpoint", "Generate Demo ID", "Normalize CRM Payload",
                     "Build Exact Raw Body", "Create Webhook Timestamp", "Sign Webhook", "Send Deal-Won Event",
                     "Validate First Response", "Create Replay Timestamp", "Sign Replay",
                     "Replay Deal-Won Event", "Validate Replay", "Present Safe Demo Result"]
    # Transport failures go to a dedicated error branch, not into the validators.
    for http, handler in (("Send Deal-Won Event", "Relay Unreachable"),
                          ("Replay Deal-Won Event", "Replay Relay Unreachable")):
        assert nodes[http]["onError"] == "continueErrorOutput"
        assert workflow["connections"][http]["main"][1][0]["node"] == handler
    assert workflow["active"] is False


def test_workflow_keeps_no_execution_data(workflow) -> None:
    assert workflow["settings"] == {"executionOrder": "v1", "saveManualExecutions": False,
                                    "saveDataErrorExecution": "none", "saveDataSuccessExecution": "none",
                                    "saveExecutionProgress": False}
    assert workflow["pinData"] == {}


def test_http_nodes_send_the_built_body_raw_and_signed(nodes) -> None:
    for name in ("Send Deal-Won Event", "Replay Deal-Won Event"):
        p = nodes[name]["parameters"]
        assert p["method"] == "POST"
        # A fixed literal (no leading "="): not an expression, nothing configurable.
        assert p["url"] == RELAY_ENDPOINT
        assert p.get("sendQuery") is not True and "proxy" not in p["options"]
        assert p["options"]["redirect"]["redirect"]["followRedirects"] is False
        assert (p["contentType"], p["rawContentType"], p["body"]) == ("raw", "application/json", "={{ $json.body }}")
        headers = {h["name"]: h["value"] for h in p["headerParameters"]["parameters"]}
        assert headers == {"X-LTD-Timestamp": "={{ $json.timestamp }}",
                           "X-LTD-Signature": "=sha256={{ $json.signature }}"}
        response = p["options"]["response"]["response"]
        assert response["fullResponse"] and response["neverError"]
        # Must stay autodetect: with a raw body, an explicit "json" format leaves the
        # response body as an unparsed stream in n8n 2.41.7 (found in the live run).
        assert "responseFormat" not in response
    for name in ("Sign Webhook", "Sign Replay"):
        p = nodes[name]["parameters"]
        assert (p["action"], p["type"], p["encoding"]) == ("hmac", "SHA256", "hex")
        assert p["value"] == "={{ $json.timestamp + '.' + $json.body }}"
        assert nodes[name]["credentials"] == {"crypto": {"id": "ltdCrmHmacDemo01",
                                                         "name": "LTD CRM webhook HMAC (demo)"}}


def test_export_contains_no_secret_or_token_material(workflow) -> None:
    text = WORKFLOW_PATH.read_text()
    assert "$env" not in text and "process.env" not in text
    assert "hmacSecret" not in text and SECRET not in text
    assert "#t=" not in text
    assert not re.search(r"(?i)(secret|token|password|api[_-]?key)\"\s*:\s*\"[^\"]", text)
    assert not re.search(r"\b[0-9a-f]{64}\b", text), "no literal signatures/keys"
    assert "require(" not in text, "Code nodes need no external modules"
    for node in workflow["nodes"]:
        if node["type"] == "n8n-nodes-base.set":
            names = {a["name"] for a in node["parameters"]["assignments"]["assignments"]}
            assert names == {"crm_format"}, "the only setting is the CRM format; the endpoint is fixed"
    assert "relay_url" not in text and "backend_url" not in text


def test_embedded_examples_are_the_documented_examples(nodes) -> None:
    code = code_of(nodes, "Normalize CRM Payload")
    embedded = json.loads(code.split("const EXAMPLES = ", 1)[1].split(";\n\n", 1)[0])
    assert embedded == {name: json.loads(path.read_text()) for name, path in EXAMPLES.items()}


# ---------------------------------------------------------- Code-node logic
def normalize(nodes, crm_format: str, demo_id: str = DEMO_ID, code: str | None = None):
    return run_code(code or code_of(nodes, "Normalize CRM Payload"),
                    nodes={"Select CRM Example": [{"crm_format": crm_format}],
                           "Generate Demo ID": [{"demo_id": demo_id}]})


@needs_node
@pytest.mark.parametrize("crm_format,prefix", [("generic", "gen"), ("gohighlevel", "ghl"), ("hubspot", "hs")])
def test_normalization_produces_a_valid_contract_payload(nodes, crm_format, prefix) -> None:
    out = ok(normalize(nodes, crm_format))[0]
    assert set(out) == {"crm_format", "normalized"} and out["crm_format"] == crm_format
    event = DealWonEvent.model_validate(out["normalized"])  # the backend's own schema
    src = json.loads(EXAMPLES[crm_format].read_text())
    assert event.event_id == f"n8n-demo-{prefix}-{DEMO_ID}"
    assert event.source == src["source"] == crm_format
    assert event.deal.external_id == f"{src['deal']['external_id']}-{DEMO_ID}"
    assert event.deal.service_type == src["deal"]["service_type"]
    assert (event.contact.name, event.contact.email) == (src["contact"]["name"], src["contact"]["email"])
    assert event.contact.external_id == src["contact"]["external_id"]


@needs_node
def test_each_execution_gets_new_event_and_deal_ids(nodes) -> None:
    a = ok(normalize(nodes, "hubspot", DEMO_ID))[0]["normalized"]
    b = ok(normalize(nodes, "hubspot", OTHER_DEMO_ID))[0]["normalized"]
    assert a["event_id"] != b["event_id"] and a["deal"]["external_id"] != b["deal"]["external_id"]
    assert a["contact"] == b["contact"], "the synthetic contact stays the same client"


@needs_node
@pytest.mark.parametrize("mutation,expected", [
    (lambda e: e["hubspot"]["deal"].update(value=10.005), "deal.value must have at most 2 decimals"),
    (lambda e: e["hubspot"]["deal"].update(value=-1), "deal.value must be a non-negative number"),
    (lambda e: e["hubspot"]["deal"].update(currency="EURO"), "deal.currency must be at most 3 characters"),
    (lambda e: e["hubspot"]["deal"].update(service_type="logo_design"), "deal.service_type must be one of"),
    (lambda e: e["hubspot"]["contact"].update(name="  "), "contact.name is required"),
    (lambda e: e["hubspot"]["contact"].update(email=None, external_id=None), "contact needs external_id or email"),
    (lambda e: e["hubspot"]["contact"].update(email="not-an-email"), "contact.email is not an email address"),
    (lambda e: e["hubspot"].update(source="generic"), 'source must be "hubspot"'),
    (lambda e: e["hubspot"].update(event_type="deal.lost"), "event_type must be deal.won"),
])
def test_normalization_rejects_invalid_examples_with_clear_errors(nodes, mutation, expected) -> None:
    code = code_of(nodes, "Normalize CRM Payload")
    head, rest = code.split("const EXAMPLES = ", 1)
    examples_json, tail = rest.split(";\n\n", 1)
    examples = json.loads(examples_json)
    mutation(examples)
    result = normalize(nodes, "hubspot", code=f"{head}const EXAMPLES = {json.dumps(examples)};\n\n{tail}")
    assert not result["ok"]
    assert result["error"].startswith("Normalization failed: ") and expected in result["error"]


@needs_node
@pytest.mark.parametrize("settings,expected", [
    ({"crm_format": "salesforce"}, "crm_format must be generic, gohighlevel or hubspot"),
    ({"crm_format": ""}, "crm_format must be"),
    ({"demo_id": "not-a-uuid"}, "no demo ID was generated"),
])
def test_normalization_rejects_bad_settings(nodes, settings, expected) -> None:
    kwargs = {"crm_format": "generic", **settings}
    result = normalize(nodes, **kwargs)
    assert not result["ok"] and expected in result["error"]


@needs_node
def test_body_is_serialized_once_and_carried_unchanged(nodes) -> None:
    normalized = ok(normalize(nodes, "gohighlevel"))
    built = ok(run_code(code_of(nodes, "Build Exact Raw Body"), input=normalized))[0]
    assert json.loads(built["body"]) == normalized[0]["normalized"]
    assert built["body"] == json.dumps(normalized[0]["normalized"], separators=(",", ":"), ensure_ascii=False)
    assert set(built) == {"body", "event_id", "deal_id", "crm_format"}
    stamped = ok(run_code(code_of(nodes, "Create Webhook Timestamp"), input=[built]))[0]
    assert stamped["body"] == built["body"] and re.fullmatch(r"\d{10}", stamped["timestamp"])
    # The replay reuses the exact same string, with a strictly later timestamp.
    replay = ok(run_code(code_of(nodes, "Create Replay Timestamp"),
                         nodes={"Build Exact Raw Body": [built], "Create Webhook Timestamp": [stamped]}))[0]
    assert replay["body"] == built["body"] and replay["event_id"] == built["event_id"]
    assert int(replay["timestamp"]) > int(stamped["timestamp"])


@needs_node
def test_crypto_node_hmac_matches_backend_signature() -> None:
    body = '{"event_id":"n8n-demo-gen-x","note":"café – \U0001f600"}'
    ts = "1767225600"
    assert "sha256=" + node_hmac(SECRET, f"{ts}.{body}") == webhook_signature(SECRET, ts, body.encode("utf-8"))


VALIDATION_CASES = [
    (401, {"status": "error", "code": "invalid_signature"}, "HTTP 401 (invalid_signature): signature rejected"),
    (422, {"status": "error", "field": "body.deal.currency"}, "HTTP 422 at body.deal.currency"),
    (200, {"status": "duplicate", "event_id": "e"}, "HTTP 200: the event was already processed earlier"),
    (409, {"status": "error", "code": "event_id_reused"}, "HTTP 409 (event_id_reused): conflict"),
    (503, {"status": "error", "code": "webhook_not_configured"}, "HTTP 503 (webhook_not_configured)"),
    (500, "Internal Server Error", "HTTP 500: unexpected response"),
    (502, {"status": "error", "code": "upstream_unreachable", "relay": True}, "HTTP 502 (upstream_unreachable): the relay could not reach the API"),
]


@needs_node
@pytest.mark.parametrize("status,body,expected", VALIDATION_CASES)
def test_first_response_failures_stop_before_the_replay(nodes, status, body, expected) -> None:
    result = run_code(code_of(nodes, "Validate First Response"), input=[{"statusCode": status, "body": body}],
                      nodes={"Build Exact Raw Body": [{"event_id": "e"}]})
    assert not result["ok"]
    assert result["error"].startswith("First request failed: ") and expected in result["error"]
    assert result["error"].endswith("Replay was not sent.")


@needs_node
def test_first_response_output_keeps_only_ids(nodes) -> None:
    response = {"statusCode": 201, "body": {"status": "created", "client_id": "c1", "invitation_id": "i1"}}
    out = ok(run_code(code_of(nodes, "Validate First Response"), input=[response],
                      nodes={"Build Exact Raw Body": [{"event_id": "e"}]}))
    assert out == [{"event_id": "e", "first_status": 201, "first_result": "created",
                    "client_id": "c1", "invitation_id": "i1"}]


@needs_node
@pytest.mark.parametrize("name", ["Validate First Response", "Validate Replay"])
def test_validators_stop_on_a_response_that_bypassed_the_relay(nodes, name) -> None:
    """Second line of defence: if a request ever bypassed the relay, stop at once and
    pass nothing on (the editor would already have shown that HTTP output)."""
    response = {"statusCode": 201, "body": {"status": "created", "client_id": "c1", "invitation_id": "i1",
                "onboarding_url": "https://hub.example.test/onboarding#t=SYNTHETIC", "expires_at": "x"}}
    result = run_code(code_of(nodes, name), input=[response], nodes={"Build Exact Raw Body": [{"event_id": "e"}]})
    assert not result["ok"]
    assert result["error"].startswith("Unsafe response: 2 field(s) outside the relay allowlist")
    assert "SYNTHETIC" not in result["error"] and "onboarding" not in result["error"]


@needs_node
@pytest.mark.parametrize("status,body,expected", [
    (201, {"status": "created"}, "accepted the replay as a NEW event"),
    (200, {"status": "duplicate_deal", "event_id": "e"}, "expected HTTP 200 duplicate, got HTTP 200 (duplicate_deal)"),
    (200, {"status": "duplicate", "event_id": "other"}, "expected HTTP 200 duplicate, got HTTP 200 (duplicate)"),
    (401, {"code": "timestamp_out_of_window"}, "got HTTP 401 (timestamp_out_of_window)"),
])
def test_replay_validation(nodes, status, body, expected) -> None:
    result = run_code(code_of(nodes, "Validate Replay"), input=[{"statusCode": status, "body": body}],
                      nodes={"Build Exact Raw Body": [{"event_id": "e"}]})
    assert not result["ok"] and expected in result["error"]


@needs_node
@pytest.mark.parametrize("name", ["Relay Unreachable", "Replay Relay Unreachable"])
def test_unreachable_relay_reports_a_safe_error(nodes, name) -> None:
    result = run_code(code_of(nodes, name), input=[{"error": {"code": "ECONNREFUSED", "message": "connect"}}])
    assert not result["ok"]
    assert result["error"] == (f"Relay not reachable at {RELAY_ORIGIN}: ECONNREFUSED. "
                               "Start the relay and the API (see docs/n8n-demo.md).")


# ----------------------------------- the relay endpoint check (fail closed)
def with_change(nodes, change) -> dict[str, dict]:
    """A copy of the exported workflow's nodes with one configuration change applied."""
    changed = copy.deepcopy(nodes)
    change(changed)
    return changed


def set_url(url, *targets):
    return lambda n: [n[t]["parameters"].update(url=url) for t in (targets or HTTP_NODES)]


BAD_ENDPOINTS = {
    "the API itself": set_url("http://127.0.0.1:8010/api/webhooks/crm/deal-won"),
    "API, first node only": set_url("http://127.0.0.1:8010/api/webhooks/crm/deal-won", "Send Deal-Won Event"),
    "API, replay node only": set_url("http://127.0.0.1:8010/api/webhooks/crm/deal-won", "Replay Deal-Won Event"),
    "localhost instead of 127.0.0.1": set_url("http://localhost:8011/api/webhooks/crm/deal-won"),
    "IPv6 loopback": set_url("http://[::1]:8011/api/webhooks/crm/deal-won"),
    "https": set_url("https://127.0.0.1:8011/api/webhooks/crm/deal-won"),
    "other port": set_url("http://127.0.0.1:8012/api/webhooks/crm/deal-won"),
    "default port": set_url("http://127.0.0.1/api/webhooks/crm/deal-won"),
    "private address": set_url("http://10.0.0.5:8011/api/webhooks/crm/deal-won"),
    "public host": set_url("https://hub.example.test/api/webhooks/crm/deal-won"),
    "other path": set_url("http://127.0.0.1:8011/api/webhooks/crm/other"),
    "trailing slash": set_url("http://127.0.0.1:8011/api/webhooks/crm/deal-won/"),
    "origin only": set_url("http://127.0.0.1:8011"),
    "query string": set_url("http://127.0.0.1:8011/api/webhooks/crm/deal-won?x=1"),
    "fragment": set_url("http://127.0.0.1:8011/api/webhooks/crm/deal-won#x"),
    "credentials in URL": set_url("http://user:pw-SENSITIVE@127.0.0.1:8011/api/webhooks/crm/deal-won"),
    "leading space": set_url(" http://127.0.0.1:8011/api/webhooks/crm/deal-won"),
    "upper-case scheme": set_url("HTTP://127.0.0.1:8011/api/webhooks/crm/deal-won"),
    "expression": set_url("={{ $json.relay_url }}/api/webhooks/crm/deal-won"),
    "expression of the right URL": set_url("=http://127.0.0.1:8011/api/webhooks/crm/deal-won"),
    "empty": set_url(""),
    "GET": lambda n: n["Send Deal-Won Event"]["parameters"].update(method="GET"),
    "query parameters": lambda n: n["Replay Deal-Won Event"]["parameters"].update(
        sendQuery=True, queryParameters={"parameters": [{"name": "x", "value": "1"}]}),
    "proxy": lambda n: n["Send Deal-Won Event"]["parameters"]["options"].update(proxy="http://127.0.0.1:8010"),
    "follows redirects": lambda n: n["Replay Deal-Won Event"]["parameters"]["options"].update(
        redirect={"redirect": {"followRedirects": True}}),
    "redirect option removed": lambda n: n["Send Deal-Won Event"]["parameters"]["options"].pop("redirect"),
    "HTTP node renamed": lambda n: n.update({"Send Deal-Won Event (copy)": n.pop("Send Deal-Won Event")}),
}


def check_endpoint(nodes):
    return run_code(code_of(nodes, "Check Relay Endpoint"), input=[{"crm_format": "generic"}],
                    params=params_of(nodes))


def test_every_path_to_an_http_node_passes_the_endpoint_check(workflow, nodes) -> None:
    """Nothing can reach either HTTP node without first passing Check Relay Endpoint."""
    reachable, todo = set(), ["Run Demo"]
    while todo:
        current = todo.pop()
        if current in reachable or current == "Check Relay Endpoint":
            continue
        reachable.add(current)
        for branch in workflow["connections"].get(current, {}).get("main", []):
            todo.extend(t["node"] for t in branch)
    assert reachable == {"Run Demo", "Select CRM Example"}
    assert [n["name"] for n in workflow["nodes"] if n["type"] == "n8n-nodes-base.manualTrigger"] == ["Run Demo"]
    inputs = [src for src, out in workflow["connections"].items() for branch in out["main"]
              for t in branch if t["node"] == "Check Relay Endpoint"]
    assert inputs == ["Select CRM Example"]
    assert "onError" not in nodes["Check Relay Endpoint"], "a failed check must stop the run"
    assert RELAY_ENDPOINT in code_of(nodes, "Check Relay Endpoint")


@needs_node
def test_endpoint_check_passes_the_exported_workflow_unchanged(nodes) -> None:
    assert ok(check_endpoint(nodes)) == [{"crm_format": "generic"}]


@needs_node
@pytest.mark.parametrize("change", BAD_ENDPOINTS.values(), ids=BAD_ENDPOINTS.keys())
def test_endpoint_check_rejects_any_other_configuration(nodes, change) -> None:
    result = check_endpoint(with_change(nodes, change))
    assert not result["ok"]
    assert result["error"].startswith("Relay endpoint check failed: ")
    assert result["error"].endswith("No request was sent.")
    assert "SENSITIVE" not in result["error"], "the rejected URL is never echoed"


# ------------------------- whole chain: n8n code -> relay -> real API (ltd_test)
def http_node(node: dict, relay_url: str, item: dict, signature: str) -> dict:
    """What an HTTP node does: POST the body string unchanged, raw, with the signed headers,
    to the URL in the node's own parameters; returns what n8n's HTTP node outputs.
    The approved endpoint is mapped to the test relay (which runs on a free port);
    any other URL is called as configured, so a missing check would really reach it."""
    url = node["parameters"]["url"]
    target = relay_url + demo_relay.PATH if url == RELAY_ENDPOINT else url
    status, headers, raw = post_raw(target, item["body"].encode("utf-8"), {
        "Content-Type": "application/json", "X-LTD-Timestamp": item["timestamp"],
        "X-LTD-Signature": f"sha256={signature}"}, path="")
    return {"statusCode": status, "headers": headers, "body": json.loads(raw), "raw": raw}


def run_demo(nodes, relay_url: str, crm_format: str, demo_id: str, secret: str = SECRET, *,
             skip_check: bool = False):
    """Run the workflow's chain in n8n's order, using the given nodes' code and parameters;
    returns (present output or error result, every HTTP node output)."""
    ran: dict[str, list[dict]] = {"Select CRM Example": [{"crm_format": crm_format}]}
    if not skip_check:  # skip_check exists only for the negative control below
        result = run_code(code_of(nodes, "Check Relay Endpoint"), input=ran["Select CRM Example"],
                          params=params_of(nodes))
        if not result["ok"]:
            return result, []
        ran["Check Relay Endpoint"] = result["items"]
    ran["Generate Demo ID"] = [{"demo_id": demo_id}]
    ran["Normalize CRM Payload"] = ok(run_code(code_of(nodes, "Normalize CRM Payload"), nodes=ran))
    ran["Build Exact Raw Body"] = ok(run_code(code_of(nodes, "Build Exact Raw Body"),
                                              input=ran["Normalize CRM Payload"]))
    stamped = ok(run_code(code_of(nodes, "Create Webhook Timestamp"), input=ran["Build Exact Raw Body"]))
    ran["Create Webhook Timestamp"] = stamped
    http_outputs = [http_node(nodes["Send Deal-Won Event"], relay_url, stamped[0],
                              node_hmac(secret, f"{stamped[0]['timestamp']}.{stamped[0]['body']}"))]
    as_n8n = lambda out: {k: out[k] for k in ("statusCode", "headers", "body")}  # noqa: E731
    result = run_code(code_of(nodes, "Validate First Response"), input=[as_n8n(http_outputs[0])], nodes=ran)
    if not result["ok"]:
        return result, http_outputs
    ran["Validate First Response"] = result["items"]
    replay_item = ok(run_code(code_of(nodes, "Create Replay Timestamp"), nodes=ran))[0]
    assert replay_item["body"] == stamped[0]["body"] and replay_item["timestamp"] != stamped[0]["timestamp"]
    http_outputs.append(http_node(nodes["Replay Deal-Won Event"], relay_url, replay_item,
                                  node_hmac(secret, f"{replay_item['timestamp']}.{replay_item['body']}")))
    result = run_code(code_of(nodes, "Validate Replay"), input=[as_n8n(http_outputs[1])], nodes=ran)
    if not result["ok"]:
        return result, http_outputs
    ran["Validate Replay"] = result["items"]
    return run_code(code_of(nodes, "Present Safe Demo Result"), nodes=ran), http_outputs


@pytest.fixture
def stack(db_conn, migrated_database, pool_cleanup):
    """(API URL, relay URL): the real API on ltd_test and the relay in front of it."""
    with live_api(migrated_database) as api, running_relay(api) as url:
        yield api, url


@pytest.fixture
def relay(stack):
    return stack[1]


@pytest.fixture
def relay_hits(monkeypatch):
    """Counts every request the relay receives (any path, any method)."""
    hits: list[str] = []
    for method in ("do_POST", "do_GET"):
        real = getattr(demo_relay.RelayHandler, method)
        monkeypatch.setattr(demo_relay.RelayHandler, method,
                            lambda self, _real=real: (hits.append(self.command), _real(self))[1])
    return hits


@pytest.fixture
def api_responses(monkeypatch):
    """Records, in memory, the API responses the relay stripped (to prove what was removed)."""
    seen: list[bytes] = []
    real = demo_relay.sanitize
    monkeypatch.setattr(demo_relay, "sanitize", lambda status, body: (seen.append(body), real(status, body))[1])
    return seen


def token_of(api_body: bytes) -> tuple[str, str]:
    url = json.loads(api_body)["onboarding_url"]
    return url, url.split("#t=", 1)[1]


@needs_node
def test_all_three_formats_create_once_and_replay_as_duplicate(nodes, relay, db_conn, api_responses) -> None:
    for i, crm_format in enumerate(("generic", "gohighlevel", "hubspot")):
        result, http_outputs = run_demo(nodes, relay, crm_format, f"{i:08d}-4b1d-4c8e-9a51-3e7d2b8f1c04")
        out = ok(result)[0]
        url, token = token_of(api_responses[-2])  # the API's 201 really contained the link...
        for http in http_outputs:  # ...but no HTTP node output contains it (the editor shows these)
            assert_safe_relay_response(http["headers"], http["raw"], url, token)
        assert [h["statusCode"] for h in http_outputs] == [201, 200]
        assert http_outputs[0]["body"] == {"status": "created", "client_id": out["first_request"]["client_id"],
                                           "invitation_id": out["first_request"]["invitation_id"]}
        assert http_outputs[1]["body"] == {"status": "duplicate", "event_id": out["event_id"]}
        assert out["result"] == "success" and out["crm_format"] == crm_format
        assert out["first_request"]["status"] == 201 and out["replay"] == {"status": 200, "result": "duplicate"}
        rendered = json.dumps(out)
        assert "#t=" not in rendered and "onboarding" not in rendered and token not in rendered

    events = db_conn.execute("SELECT external_event_id, source, status, duplicate_count FROM webhook_events "
                             "ORDER BY source").fetchall()
    assert [(e["source"], e["status"], e["duplicate_count"]) for e in events] == [
        ("generic", "processed", 1), ("gohighlevel", "processed", 1), ("hubspot", "processed", 1)]
    assert all(e["external_event_id"].startswith("n8n-demo-") for e in events)
    assert counts(db_conn)["clients"] == 3 and counts(db_conn)["onboarding_invitations"] == 3


@needs_node
def test_second_execution_is_a_new_event_for_the_same_client(nodes, relay, db_conn) -> None:
    first_out = ok(run_demo(nodes, relay, "generic", DEMO_ID)[0])[0]
    second_out = ok(run_demo(nodes, relay, "generic", OTHER_DEMO_ID)[0])[0]
    assert first_out["event_id"] != second_out["event_id"] and first_out["deal_id"] != second_out["deal_id"]
    assert first_out["first_request"]["client_id"] == second_out["first_request"]["client_id"]
    assert counts(db_conn)["webhook_events"] == 2 and counts(db_conn)["clients"] == 1


@needs_node
def test_wrong_credential_secret_is_rejected_and_nothing_is_written(nodes, relay, db_conn) -> None:
    result, http_outputs = run_demo(nodes, relay, "generic", DEMO_ID, secret="a-different-secret-" + "y" * 32)
    assert http_outputs[0]["statusCode"] == 401 and http_outputs[0]["body"] == {"status": "error",
                                                                                 "code": "invalid_signature"}
    assert not result["ok"] and "signature rejected" in result["error"]
    assert counts(db_conn) == {table: 0 for table in INTAKE_TABLES}


@needs_node
@pytest.mark.parametrize("target", ["api", "relay_other_path", "fake"])
def test_misconfigured_endpoint_reaches_neither_the_relay_nor_the_api(
        nodes, stack, db_conn, api_responses, relay_hits, target) -> None:
    """The exported workflow with an HTTP node pointed somewhere else stops at the
    check: no request reaches the relay, the API or the configured target, and nothing
    is written. The targets are live and would answer if they were called."""
    api, relay_url = stack
    with FakeUpstream(201, {"status": "created"}) as fake:
        url = {"api": api + demo_relay.PATH, "relay_other_path": relay_url + "/api/webhooks/crm/other",
               "fake": fake.url + demo_relay.PATH}[target]
        result, http_outputs = run_demo(with_change(nodes, set_url(url)), relay_url, "generic", DEMO_ID)
        assert not result["ok"] and result["error"].startswith("Relay endpoint check failed: ")
        assert http_outputs == [] and fake.requests == []
    assert relay_hits == [] and api_responses == []
    assert counts(db_conn) == {table: 0 for table in INTAKE_TABLES}


@needs_node
def test_replay_node_misconfigured_alone_stops_before_the_first_request(
        nodes, stack, db_conn, api_responses, relay_hits) -> None:
    """The check covers both HTTP nodes up front: a bad replay URL stops the run
    before the first request, so nothing half-happens."""
    api, relay_url = stack
    changed = with_change(nodes, set_url(api + demo_relay.PATH, "Replay Deal-Won Event"))
    result, http_outputs = run_demo(changed, relay_url, "generic", DEMO_ID)
    assert not result["ok"] and "Replay Deal-Won Event: URL must be exactly" in result["error"]
    assert "Send Deal-Won Event" not in result["error"]
    assert http_outputs == [] and relay_hits == [] and api_responses == []
    assert counts(db_conn) == {table: 0 for table in INTAKE_TABLES}


@needs_node
def test_correct_configuration_reaches_the_relay(nodes, stack, db_conn, api_responses, relay_hits) -> None:
    """Positive counterpart of the tests above: the same spies do see the real run."""
    result, http_outputs = run_demo(nodes, stack[1], "generic", DEMO_ID)
    assert ok(result)[0]["result"] == "success"
    assert relay_hits == ["POST", "POST"] and len(api_responses) == 2


@needs_node
def test_negative_control_without_the_check_the_api_link_reaches_the_http_node(
        nodes, stack, db_conn, api_responses) -> None:
    """Negative control: the same misconfiguration with the check skipped really does
    reach the API, and the HTTP node's output (what the editor shows) carries the
    invitation link. This is exactly what Check Relay Endpoint prevents."""
    api, relay_url = stack
    result, http_outputs = run_demo(with_change(nodes, set_url(api + demo_relay.PATH)), relay_url, "generic",
                                    DEMO_ID, skip_check=True)
    assert len(http_outputs) == 1 and http_outputs[0]["statusCode"] == 201
    assert "#t=" in http_outputs[0]["body"]["onboarding_url"]
    with pytest.raises(AssertionError, match="leaks"):
        assert_safe_relay_response(http_outputs[0]["headers"], http_outputs[0]["raw"])
    assert not result["ok"] and result["error"].startswith("Unsafe response")  # only the validator stopped it
