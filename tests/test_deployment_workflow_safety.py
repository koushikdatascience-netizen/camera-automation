from pathlib import Path

import yaml


def test_production_cloud_deploy_is_manual_and_uses_protected_environment():
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cloud-deploy.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert list(workflow["on"]) == ["workflow_dispatch"]
    deploy = workflow["jobs"]["deploy"]
    assert deploy["environment"]["name"] == "production"


def test_deploy_workflow_does_not_automatically_push_production():
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cloud-deploy.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert "push" not in workflow["on"]
    assert "pull_request" not in workflow["on"]
