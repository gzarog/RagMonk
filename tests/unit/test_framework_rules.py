from __future__ import annotations

from ragmonk.code import framework_rules
from ragmonk.code.extractor import ExtractedDecorator
from ragmonk.core.models import Confidence, RelationshipType


def test_python_flask_route_decorator_is_heuristic() -> None:
    decorators = [ExtractedDecorator(1, '@app.route("/dogs", methods=["GET"])', 10)]
    findings = framework_rules.detect_python_route_decorators(decorators)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.confidence is Confidence.HEURISTIC
    assert finding.confidence is not Confidence.EXACT
    assert finding.relationship_type is RelationshipType.REFERENCES
    assert finding.target_symbol == "http_endpoint:GET:/dogs"


def test_python_fastapi_get_decorator_defaults_method_from_verb() -> None:
    decorators = [ExtractedDecorator(1, '@app.get("/cats")', 5)]
    findings = framework_rules.detect_python_route_decorators(decorators)
    assert findings[0].target_symbol == "http_endpoint:GET:/cats"


def test_python_unrelated_decorator_is_not_a_route() -> None:
    decorators = [ExtractedDecorator(1, "@staticmethod", 1), ExtractedDecorator(1, "@dataclass", 2)]
    assert framework_rules.detect_python_route_decorators(decorators) == []


def test_csharp_route_attribute_is_heuristic() -> None:
    decorators = [ExtractedDecorator(1, 'Route("api/dogs/{id}")', 3)]
    findings = framework_rules.detect_csharp_route_attributes(decorators)
    assert len(findings) == 1
    assert findings[0].confidence is Confidence.HEURISTIC
    assert findings[0].target_symbol == "http_endpoint:ANY:api/dogs/{id}"


def test_csharp_http_get_attribute_sets_method() -> None:
    decorators = [ExtractedDecorator(1, "HttpGet", 3)]
    findings = framework_rules.detect_csharp_route_attributes(decorators)
    assert findings[0].target_symbol == "http_endpoint:GET:"


def test_csharp_unrelated_attribute_is_not_a_route() -> None:
    decorators = [ExtractedDecorator(1, "Obsolete", 1)]
    assert framework_rules.detect_csharp_route_attributes(decorators) == []


def test_detect_dispatches_by_language_and_is_empty_for_others() -> None:
    py_findings = framework_rules.detect(
        "python", [ExtractedDecorator(1, '@app.post("/x")', 1)]
    )
    assert len(py_findings) == 1
    assert framework_rules.detect("go", [ExtractedDecorator(1, "whatever", 1)]) == []


def test_all_findings_are_heuristic_never_exact() -> None:
    all_findings = framework_rules.detect_python_route_decorators(
        [ExtractedDecorator(1, '@app.route("/a")', 1)]
    ) + framework_rules.detect_csharp_route_attributes([ExtractedDecorator(1, "HttpPost", 1)])
    assert all(f.confidence is Confidence.HEURISTIC for f in all_findings)
