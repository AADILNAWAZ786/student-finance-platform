"""Render API.md (Phase 9, P9-D5) from three sources, none of them hand-written:

* the OpenAPI schema the code generates (drf-spectacular): every route, method, summary,
  description, parameter, request field and documented error example (each error example is
  replayed against the live API by ``tests/verification/test_verify_openapi_examples.py``);
* the transcript ``demo_workflow`` captured from the running application (real requests and
  responses; ARCHITECTURE §9.3: "captured from the running app, not hand-written");
* the permission table ``demo_workflow`` measured by calling every operation as every role.

``tests/datagen/test_demo_workflow.py::test_api_md_is_rendered_from_the_schema_and_the_demo``
re-renders it from a fresh demo run and requires API.md to be identical; run that test with
``SF_UPDATE_API_DOC=1`` to rewrite API.md after an intended change.
"""

from __future__ import annotations

import json
import re

TAG_ORDER = [
    "health",
    "students",
    "programs",
    "terms",
    "enrollments",
    "fee-schedules",
    "invoices",
    "billing",
    "scholarship-types",
    "eligibility",
    "scholarships",
    "payments",
    "payment-allocations",
    "refunds",
    "balance-snapshots",
    "reconciliation",
    "anomalies",
    "datagen",
]
METHODS = ("get", "post", "patch", "put", "delete")
ROLES = ("platform_admin", "academic_admin", "support_staff", "instructor", "learner")
COLLAPSE_LINES = 30

BASICS = [
    (
        "Base URL",
        "`http://127.0.0.1:8000/api/v1/` with `python manage.py runserver`; every path below "
        "includes `/api/v1/`.",
    ),
    (
        "Content type",
        "Requests and responses are `application/json` (UTF-8). The browsable API also renders "
        "HTML for browsers.",
    ),
    (
        "Authentication",
        "DRF token: header `Authorization: Token <key>`. Session login also works (browsable "
        "API, Swagger). Every operation needs an authenticated user except "
        "`GET /api/v1/health/`. Without credentials: 401 `NOT_AUTHENTICATED`; with a role that "
        "may not call the operation: 403 `PERMISSION_DENIED`.",
    ),
    (
        "Getting a token",
        "`python manage.py seed_roles --demo-users` creates the five role groups and one "
        "`demo_<role>` user per role and prints each user's token (development only). For "
        "another user: `python manage.py drf_create_token <username>` (Django REST Framework). "
        "Superusers pass every role check.",
    ),
    (
        "Money",
        'Strings with 2 decimal places, in INR (`"12500.00"`); more than 2 places → 400.',
    ),
    (
        "Dates",
        "ISO `YYYY-MM-DD`; business dates are in 2026. When a request omits a business date the "
        "service uses today's date in `Asia/Kolkata`.",
    ),
    (
        "IDs",
        "Business IDs in paths and bodies (`STU-000001`, `INV-STU-000001-2026-000001`, …; "
        "DATA_DICTIONARY.md §3).",
    ),
    (
        "Pagination",
        "List endpoints: `?page=` and `?page_size=` (default 50, maximum 500); responses are "
        "`{count, next, previous, results}`.",
    ),
    (
        "Filtering and ordering",
        "The query parameters listed for each list endpoint (django-filter), and `?ordering=`.",
    ),
    (
        "Financial records",
        "Read and action only: no PUT, PATCH or DELETE on fee schedules, invoices, lines, "
        "payments, allocations, refunds or the ledger (405). Changes are new rows: versions, "
        "adjustments, deallocations, reversals, events.",
    ),
]

NOT_OVER_HTTP = (
    "These exist only as management commands (README): full-scale generation "
    "(`generate_data --scale full`; `POST /datagen/runs/` is starter-only), `export_data`, "
    "`verify_export`, `verify_reproducibility`, `demo_workflow`, `evaluate --out` (the "
    "evaluation itself is also `GET .../evaluation/`), `seed_terms`, `seed_roles`, "
    "`seed_scholarship_types`. `/admin/` is the Django admin, not part of the API: payment, "
    "scholarship and reconciliation records are read-only there and billing records are not "
    "registered. There is no import endpoint or command."
)

NO_SUCCESS_CAPTURE = (
    "No successful call was captured: the demo's databases already hold a dataset, and this "
    "operation runs only on empty databases (the demo captures its 409 below). The 201 "
    "response is the generation run with its reconciliation run and evaluation; it is tested "
    "in `tests/datagen/test_commands_and_api.py`."
)


def schema_dict() -> dict:
    """The OpenAPI document exactly as /api/schema/ serves it, as plain JSON types."""
    from drf_spectacular.generators import SchemaGenerator
    from drf_spectacular.renderers import OpenApiJsonRenderer

    raw = SchemaGenerator().get_schema(request=None, public=True)
    return json.loads(OpenApiJsonRenderer().render(raw, renderer_context={}))


def operations(schema: dict) -> list[tuple[str, str, dict]]:
    return [
        (method.upper(), path, item[method])
        for path, item in schema["paths"].items()
        for method in METHODS
        if method in item
    ]


def matches(path: str, template: str) -> bool:
    return re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", template), path) is not None


def _json(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _block(value, label: str) -> list[str]:
    text = _json(value)
    body = ["```json", text, "```"]
    if text.count("\n") + 1 > COLLAPSE_LINES:
        return [f"<details><summary>{label}</summary>", "", *body, "", "</details>", ""]
    return [f"{label}:", "", *body, ""]


def _resolve(schema: dict, node: dict) -> dict:
    while "$ref" in node:
        node = schema["components"]["schemas"][node["$ref"].split("/")[-1]]
    if "allOf" in node and len(node["allOf"]) == 1:
        rest = {k: v for k, v in node.items() if k != "allOf"}
        return {**_resolve(schema, node["allOf"][0]), **rest}
    return node


def _type(schema: dict, prop: dict) -> str:
    if "oneOf" in prop or "anyOf" in prop:
        parts = [_type(schema, p) for p in prop.get("oneOf", prop.get("anyOf"))]
        return " or ".join(p for p in parts if p not in ("null", "object"))
    node = _resolve(schema, prop)
    kind = node.get("type", "object")
    if "enum" in node:
        values = [v for v in node["enum"] if v not in (None, "")]
        return "one of " + ", ".join(f"`{v}`" for v in values)
    if kind == "array":
        return f"array of {_type(schema, node.get('items', {}))}"
    if node.get("format") == "decimal" or node.get("pattern", "").startswith("^-?\\d"):
        return "money string (2 dp)"
    if node.get("format"):
        return f"{kind} ({node['format']})"
    if node.get("maxLength"):
        return f"{kind} (≤ {node['maxLength']})"
    return kind


def _cell(text: str | None) -> str:
    text = re.split(r"\s?\*\s`", text or "")[0]  # drop the enum list drf-spectacular appends
    return " ".join(text.split()).replace("|", "\\|")


def _fields(schema: dict, op: dict) -> list[str]:
    content = op.get("requestBody", {}).get("content", {}).get("application/json")
    if not content:
        return []
    node = _resolve(schema, content["schema"])
    required = set(node.get("required", []))
    rows = []
    for name, prop in node.get("properties", {}).items():
        if prop.get("readOnly"):
            continue
        note = _cell(prop.get("description") or _resolve(schema, prop).get("description"))
        need = "yes" if name in required else "no"
        rows.append(f"| `{name}` | {_type(schema, prop)} | {need} | {note} |")
    if not rows:
        return []
    head = ["Request body (`application/json`):", "", "| Field | Type | Required | Notes |"]
    return [*head, "|---|---|---|---|", *rows, ""]


def _parameters(op: dict, where: str) -> list[str]:
    params = [p for p in op.get("parameters", []) if p.get("in") == where]
    if not params:
        return []
    title = {"path": "Path parameters", "query": "Query parameters"}[where]
    rows = [f"{title}:", "", "| Name | Type | Required | Description |", "|---|---|---|---|"]
    for p in params:
        typ = p.get("schema", {}).get("type", "string")
        need = "yes" if p.get("required") else "no"
        rows.append(f"| `{p['name']}` | {typ} | {need} | {_cell(p.get('description'))} |")
    return [*rows, ""]


def _roles(allowed: list[str]) -> str:
    if "anonymous" in allowed:
        return "public (no authentication)"
    return ", ".join(f"`{r}`" for r in ROLES if r in allowed) or "none"


def _target(req: dict) -> str:
    query = req["query"] or {}
    return req["path"] + ("?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "")


def _calls_for(calls: list, method: str, path: str) -> list:
    return [
        c for c in calls if c["request"]["method"] == method and matches(c["request"]["path"], path)
    ]


def _documented_errors(op: dict) -> list[str]:
    lines = []
    for code, resp in sorted(op["responses"].items()):
        if code in ("401", "403", "404") or not code.startswith("4"):
            continue
        examples = resp.get("content", {}).get("application/json", {}).get("examples", {})
        for name, ex in examples.items():
            value = json.dumps(ex["value"], ensure_ascii=False)
            lines.append(f"- {code}, {ex.get('summary') or name}: `{value}`")
    if not lines:
        return []
    intro = (
        "Documented errors (OpenAPI examples; the verification suite replays each one "
        "against the live API):"
    )
    return [intro, "", *lines, ""]


def _operation(schema, method, path, op, calls, perms) -> list[str]:
    mine = _calls_for(calls, method, path)
    ok = next((c for c in mine if c["response"]["status"] < 300), None)
    refused = [c for c in mine if c["response"]["status"] >= 400]
    out = [f"#### `{method} {path}`", ""]
    if op.get("summary"):
        out += [f"**{op['summary'].rstrip('.')}.**", ""]
    if op.get("description"):
        out += [" ".join(op["description"].split()), ""]
    public = not op.get("security") or {} in op["security"]
    out += [
        f"- Authentication: {'none' if public else 'token or session'}",
        f"- Roles: {_roles(perms[f'{method} {path}']['allowed'])}",
        f"- Status codes: {', '.join(sorted(op['responses']))}",
        "",
    ]
    out += _parameters(op, "path") + _parameters(op, "query") + _fields(schema, op)
    if ok is None:
        out += [NO_SUCCESS_CAPTURE, ""]
    else:
        out += [f"Captured request: `{method} {_target(ok['request'])}`", ""]
        if ok["request"]["body"] is not None and method != "GET":
            out += _block(ok["request"]["body"], "Request body")
        out += _block(ok["response"]["body"], f"Response {ok['response']['status']}")
    for c in refused:
        out += [f"Captured refusal, {c['title']}: `{method} {_target(c['request'])}`", ""]
        if c["request"]["body"] is not None and method != "GET":
            out += _block(c["request"]["body"], "Request body")
        out += _block(c["response"]["body"], f"Response {c['response']['status']}")
    return out + _documented_errors(op)


def _header(schema: dict, ops: list, params: dict) -> list[str]:
    from core.exceptions import API_ERROR_CODES, DOMAIN_ERROR_CODES

    envelope = {
        "error": {
            "code": "ALLOCATION_EXCEEDS_PAYMENT",
            "rule_id": "BR-ALC-01",
            "message": "…",
            "details": {},
        }
    }
    return [
        "# API reference",
        "",
        "The Student Finance REST API, every operation of it. Generated by `datagen/apidoc.py` "
        "from the OpenAPI schema the code produces, the requests and responses `demo_workflow` "
        f"captured from the running application (seed `{params['seed']}`, scale "
        f"`{params['scale']}`, business date {params['business_date']}) and the role table it "
        "measured by calling every operation as every role. Apart from this introduction's "
        "prose nothing is hand-written; a test re-renders the file from a fresh demo run and "
        "fails on any difference.",
        "",
        "> Location: at the repository root at the project owner's instruction; the frozen "
        "ARCHITECTURE §3 layout places it under `docs/`.",
        "",
        f"{len(ops)} operations on {len(schema['paths'])} paths. Interactive documentation: "
        "`/api/docs/` (Swagger UI), `/api/redoc/` (ReDoc); the schema: `/api/schema/`. "
        "Business rules: [BUSINESS_RULES.md](BUSINESS_RULES.md); fields: "
        "[DATA_DICTIONARY.md](DATA_DICTIONARY.md).",
        "",
        "Captured examples are complete responses. Only execution metadata is replaced by "
        f"`{params['mask']}`: " + ", ".join(f"`{k}`" for k in params["masked_keys"]) + ". "
        "Requests were sent with a token header, which is not recorded; no credential, "
        "cookie or secret appears in this file.",
        "",
        "## Basics",
        "",
        "| Topic | Rule |",
        "|---|---|",
        *[f"| {topic} | {rule} |" for topic, rule in BASICS],
        "",
        "## Errors",
        "",
        "Every error uses one envelope:",
        "",
        "```json",
        _json(envelope),
        "```",
        "",
        "`rule_id` names the business rule (BUSINESS_RULES.md), or is null for generic errors. "
        "400 malformed input or failed validation; 401 not authenticated; 403 role not "
        "allowed; 404 unknown ID or route; 405 method not allowed; 409 state conflict "
        "(duplicate, illegal transition, group conflict); 422 valid input that breaks a "
        "financial rule; 503 health check failing.",
        "",
        "| Code | HTTP |",
        "|---|---|",
        *[f"| `{c}` | {s} |" for c, s in {**API_ERROR_CODES, **DOMAIN_ERROR_CODES}.items()],
        "",
    ]


def render(schema: dict, transcript: dict, results: dict) -> str:
    calls, perms = transcript["calls"], results["G_permissions"]
    params = {**results["parameters"], "mask": transcript["mask"]}
    params["masked_keys"] = transcript["masked_keys"]
    ops = operations(schema)
    out = _header(schema, ops, params)
    out += [
        "## Roles",
        "",
        "Five groups (ARCHITECTURE §13). The table is measured: `demo_workflow` called every "
        "operation as each role and anonymously, before any validation, and recorded who was "
        "refused (401 / 403).",
        "",
        "| Operation | " + " | ".join(ROLES) + " | anonymous |",
        "|---|" + "---|" * (len(ROLES) + 1),
    ]
    for method, path, _op in ops:
        allowed = perms[f"{method} {path}"]["allowed"]
        marks = " | ".join("✓" if r in allowed else "" for r in (*ROLES, "anonymous"))
        out.append(f"| `{method} {path}` | {marks} |")
    out += [
        "",
        "Learners and instructors see no student or finance data: the frozen data model has no "
        'login↔student or login↔program link, so "read own record" and "enrollments for own '
        'programs" are **not available** (docs/KNOWN_LIMITATIONS.md L1, L2).',
        "",
        "## Not available over HTTP",
        "",
        NOT_OVER_HTTP,
        "",
        "## Operations",
        "",
    ]
    by_tag: dict[str, list] = {}
    for method, path, op in ops:
        by_tag.setdefault((op.get("tags") or ["other"])[0], []).append((method, path, op))
    tags = [t for t in TAG_ORDER if t in by_tag] + sorted(t for t in by_tag if t not in TAG_ORDER)
    out += [f"- [{t}](#{t})" for t in tags] + [""]
    for tag in tags:
        out += [f"### {tag}", ""]
        for method, path, op in by_tag[tag]:
            out += _operation(schema, method, path, op, calls, perms)
    return "\n".join(out).rstrip() + "\n"
