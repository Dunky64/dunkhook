import json
import sys
from pathlib import Path

from dunkhook.core.llm import ScriptedLLM
from dunkhook.scaffold import load_spec, scaffold

SPEC = Path(__file__).resolve().parent.parent / "examples" / "petstore.json"


def test_generated_scaffold_passes_its_own_tests(tmp_path):
    res = scaffold(load_spec(SPEC), tmp_path, log=lambda *_: None)
    assert res.tests_passed, res.test_output
    assert (res.operations, res.models, res.package) == (5, 4, "petstore_client")
    client_src = (tmp_path / "petstore_client" / "client.py").read_text()
    assert "def owner_pets(self, owner_id: str, include_archived: bool) -> models.PetPage:" in client_src
    assert "def list_pets(self, limit: Optional[int] = None, tag: Optional[str] = None) -> List[models.Pet]:" in client_src


def test_client_works_against_mock_in_process(tmp_path):
    scaffold(load_spec(SPEC), tmp_path, verify=False, log=lambda *_: None)
    sys.path.insert(0, str(tmp_path))
    try:
        from petstore_client import ApiError, Client, models
        from petstore_client._runtime import start_mock

        server, url = start_mock()
        c = Client(url)
        pets = c.list_pets(limit=2)
        assert isinstance(pets[0], models.Pet) and isinstance(pets[0].owner, models.Owner)
        assert pets[0].to_dict()["created-at"] == "2024-01-01T00:00:00Z"
        created = c.create_pet(models.NewPet(name="Rex"))
        assert created.id == 1
        assert c.delete_pet(1) is None
        try:
            c.create_pet({"tag": "no name"})
        except ApiError as e:
            assert e.status == 422 and e.body == {"detail": ["body.name: required"]}
        else:
            raise AssertionError("expected 422")
        server.shutdown()
    finally:
        sys.path.remove(str(tmp_path))
        for mod in [m for m in sys.modules if m.startswith("petstore_client")]:
            del sys.modules[mod]


def test_edge_case_loop_keeps_only_passing_tests(tmp_path):
    bad = "def test_wrong(client):\n    assert client.get_pet(1).name == 'definitely not'\n"
    good = (
        "from petstore_client import models\n\n"
        "def test_roundtrip(client):\n    p = client.get_pet(7)\n    assert models.Pet.from_dict(p.to_dict()) == p\n"
    )
    replies = iter([bad, good])
    prompts = []

    def handler(system, prompt, schema):
        prompts.append(prompt)
        return {"test_code": next(replies), "summary": "roundtrip"}

    res = scaffold(load_spec(SPEC), tmp_path, llm=ScriptedLLM(json_handler=handler), log=lambda *_: None)
    assert res.edge_cases_kept is True
    assert (tmp_path / "tests" / "test_edge_cases.py").read_text() == good
    assert "failed" in prompts[1] and "test_wrong" in prompts[1]


def test_edge_cases_dropped_when_they_never_pass(tmp_path):
    bad = "def test_wrong():\n    assert False\n"
    llm = ScriptedLLM(json_handler=lambda *a: {"test_code": bad, "summary": ""})
    res = scaffold(load_spec(SPEC), tmp_path, llm=llm, edge_case_rounds=2, log=lambda *_: None)
    assert res.edge_cases_kept is False and not (tmp_path / "tests" / "test_edge_cases.py").exists()
    assert len(llm.calls) == 2


def test_minimal_spec_without_components(tmp_path):
    spec = {"openapi": "3.0.0", "info": {"title": "Ping API"},
            "paths": {"/ping": {"get": {"responses": {"200": {"description": "", "content": {"application/json": {
                "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}}}}}}}}}
    res = scaffold(json.loads(json.dumps(spec)), tmp_path, log=lambda *_: None)
    assert res.tests_passed, res.test_output
    assert "def get_ping(self) -> Dict[str, Any]:" in (tmp_path / "ping_api_client" / "client.py").read_text()
