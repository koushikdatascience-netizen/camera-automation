from pathlib import Path

import yaml


def _deploy_workflow():
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cloud-deploy.yml"
    return yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_cloud_deploy_requires_manual_dispatch():
    workflow = _deploy_workflow()
    assert 'push' not in workflow['on']
    assert "workflow_dispatch" in workflow["on"]
    assert "pull_request" not in workflow["on"]


def test_cloud_deploy_requires_production_environment_approval():
    workflow = _deploy_workflow()
    deploy = workflow["jobs"]["deploy"]
    assert deploy['environment']=='production'


def test_windows_edge_update_publication_requires_manual_opt_in_and_production_environment():
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "windows-release.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert "workflow_dispatch" in workflow["on"]
    assert "push" not in workflow["on"]
    assert workflow["on"]["workflow_dispatch"]["inputs"]["publish"]["default"] == "false"
    publish = workflow["jobs"]["publish-edge-update"]
    assert publish["if"] == "${{ inputs.publish }}"
    assert publish["environment"]["name"] == "production"


def test_cloud_deploy_validates_before_production_deploy():
    workflow = _deploy_workflow()
    steps = workflow["jobs"]["deploy"]["steps"]
    names = [step.get("name", "") for step in steps]
    assert names.index("Validate cloud code") < names.index("Build tested production image")
    assert names.index("Build tested production image") < names.index("Verify production image with isolated PostgreSQL")
    assert names.index("Verify production image with isolated PostgreSQL") < names.index("Deploy exact tested image")


def test_deploy_handler_preserves_server_owned_compose():
    script=(Path(__file__).resolve().parents[1]/"tools/deploy_cloud.sh").read_text()
    assert 'install -m 0644 "$work/docker-compose.cloud.yml" "$project/docker-compose.cloud.yml"' not in script
    assert '"${compose[@]}" config --quiet' in script
    assert '--no-deps --no-build --pull never portal' in script


def test_compose_passes_fail_closed_integration_scope_allow_list():
    root=Path(__file__).resolve().parents[1]
    compose=yaml.load((root/'docker-compose.cloud.yml').read_text(),Loader=yaml.BaseLoader)
    assert compose['services']['portal']['environment']['SNAPKEY_CRM_INTEGRATION_ALLOWED_SCOPES']== '${SNAPKEY_CRM_INTEGRATION_ALLOWED_SCOPES:-[]}'
