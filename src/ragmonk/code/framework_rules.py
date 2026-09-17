"""Small, explicit, testable heuristics for framework patterns that are
not generic AST facts -- an HTTP route method inferred from a decorator
or attribute, for example.

Deliberately narrow for Phase 2: one Python (Flask/FastAPI-style route
decorator) rule and one C# (ASP.NET-style ``[Route]``/``[Http*]``
attribute) rule, per the blueprint's "a couple of well-tested examples,
not an exhaustive framework catalog." Anything found here is tagged
``Confidence.HEURISTIC`` -- never ``EXACT`` -- because recognizing
``@app.route`` as *routing* (as opposed to some unrelated decorator
named ``route``) is a naming-convention guess, not a language fact.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from ragmonk.code.extractor import ExtractedDecorator
from ragmonk.core.models import Confidence, RelationshipType

_HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})

_PY_ROUTE_CALL = re.compile(
    r"""@\s*[\w.]*\.(?P<verb>route|get|post|put|patch|delete|head|options)\s*\(\s*
        (?P<quote>['"])(?P<path>[^'"]+)(?P=quote)
        (?P<rest>.*)
    """,
    re.VERBOSE | re.DOTALL,
)
_PY_METHODS_KW = re.compile(r"""methods\s*=\s*\[\s*['"](?P<method>\w+)['"]""")

_CS_ROUTE_ATTR = re.compile(
    r"""^(?P<name>Http(?P<verb>Get|Post|Put|Patch|Delete)|Route)\s*
        (?:\(\s*(?P<quote>['"])(?P<path>[^'"]*)(?P=quote)\s*\))?
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class FrameworkFinding:
    subject_local_id: int
    relationship_type: RelationshipType
    target_symbol: str
    resolver: str
    evidence: str
    confidence: Confidence = Confidence.HEURISTIC


def detect_python_route_decorators(
    decorators: Sequence[ExtractedDecorator],
) -> list[FrameworkFinding]:
    """``@app.route("/x")`` / ``@app.get("/x")`` (Flask- and FastAPI-style)."""
    findings = []
    for dec in decorators:
        match = _PY_ROUTE_CALL.match(dec.text)
        if match is None:
            continue
        verb = match.group("verb")
        path = match.group("path")
        if verb == "route":
            methods_match = _PY_METHODS_KW.search(match.group("rest"))
            http_method = methods_match.group("method").upper() if methods_match else "GET"
        else:
            http_method = verb.upper()
        findings.append(
            FrameworkFinding(
                subject_local_id=dec.subject_local_id,
                relationship_type=RelationshipType.REFERENCES,
                target_symbol=f"http_endpoint:{http_method}:{path}",
                resolver="framework:python_route_decorator",
                evidence=dec.text,
            )
        )
    return findings


def detect_csharp_route_attributes(
    decorators: Sequence[ExtractedDecorator],
) -> list[FrameworkFinding]:
    """ASP.NET-style ``[Route("...")]`` / ``[HttpGet]`` method attributes."""
    findings = []
    for dec in decorators:
        match = _CS_ROUTE_ATTR.match(dec.text)
        if match is None:
            continue
        verb = match.group("verb")
        path = match.group("path") or ""
        http_method = verb.upper() if verb else "ANY"
        findings.append(
            FrameworkFinding(
                subject_local_id=dec.subject_local_id,
                relationship_type=RelationshipType.REFERENCES,
                target_symbol=f"http_endpoint:{http_method}:{path}",
                resolver="framework:csharp_route_attribute",
                evidence=dec.text,
            )
        )
    return findings


_RULES_BY_LANGUAGE = {
    "python": detect_python_route_decorators,
    "csharp": detect_csharp_route_attributes,
}


def detect(
    language: str, decorators: Sequence[ExtractedDecorator]
) -> list[FrameworkFinding]:
    rule = _RULES_BY_LANGUAGE.get(language)
    return rule(decorators) if rule is not None else []
