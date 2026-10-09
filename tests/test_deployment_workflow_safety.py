from pathlib import Path

import yaml


def _deploy_workflow():
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cloud-deploy.yml"
    return yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_cloud_deploy_runs_automatically_on_reviewed_feature_branch():
    workflow = _deploy_workflow()
    assert workflow["on"]["push"]["branches"] == ["feat/camera-eye-live-view-ux"]
    assert "workflow_dispatch" in workflow["on"]
    assert "pull_request" not in workflow["on"]


def test_cloud_deploy_does_not_require_environment_approval():
    workflow = _deploy_workflow()
    deploy = workflow["jobs"]["deploy"]
    assert "environment" not in deploy


def test_cloud_deploy_validates_before_production_deploy():
    workflow = _deploy_workflow()
    steps = workflow["jobs"]["deploy"]["steps"]
    names = [step.get("name", "") for step in steps]
    assert names.index("Validate cloud code") < names.index("Build tested production image")
    assert names.index("Build tested production image") < names.index("Verify production image with isolated PostgreSQL")
    assert names.index("Verify production image with isolated PostgreSQL") < names.index("Deploy exact tested image")
