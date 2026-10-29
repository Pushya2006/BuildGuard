from pathlib import Path
from fastapi.testclient import TestClient
from app.api import create_app


def test_inspect_external_project_is_read_only(tmp_path):
    projects = tmp_path / 'allowed-projects'
    runs = tmp_path / 'runs'
    source = tmp_path / 'external' / 'sample-app'
    projects.mkdir()
    source.mkdir(parents=True)
    (source / 'main.py').write_text('print("hello")\n', encoding='utf-8')
    (source / 'test_main.py').write_text('def test_ok():\n    assert True\n', encoding='utf-8')
    app = create_app(projects, runs, allow_test_client=True)
    client = TestClient(app, base_url='http://localhost')
    response = client.post('/api/projects/inspect', json={'path': str(source)})
    assert response.status_code == 200, response.text
    assert response.json()['name'] == 'sample-app'
    assert (source / 'main.py').read_text(encoding='utf-8') == 'print("hello")\n'
    assert list(projects.iterdir()) == []


def test_import_external_project_creates_working_copy_and_preserves_original(tmp_path):
    projects = tmp_path / 'allowed-projects'
    runs = tmp_path / 'runs'
    source = tmp_path / 'external' / 'sample-app'
    projects.mkdir()
    source.mkdir(parents=True)
    (source / 'main.py').write_text('ORIGINAL\n', encoding='utf-8')
    (source / 'test_main.py').write_text('def test_ok():\n    assert True\n', encoding='utf-8')
    (source / '.venv').mkdir()
    (source / '.venv' / 'large.bin').write_bytes(b'x')
    app = create_app(projects, runs, allow_test_client=True)
    client = TestClient(app, base_url='http://localhost')
    response = client.post('/api/projects/import', json={'path': str(source)})
    assert response.status_code == 200, response.text
    data = response.json()
    copied = Path(data['working_copy'])
    assert copied.is_dir()
    assert (copied / 'main.py').read_text(encoding='utf-8') == 'ORIGINAL\n'
    assert not (copied / '.venv').exists()
    assert (source / 'main.py').read_text(encoding='utf-8') == 'ORIGINAL\n'
    assert data['project'] in [p['name'] for p in client.get('/api/projects').json()['projects']]


def test_import_rejects_missing_path(tmp_path):
    projects = tmp_path / 'allowed-projects'
    projects.mkdir()
    app = create_app(projects, tmp_path / 'runs', allow_test_client=True)
    client = TestClient(app, base_url='http://localhost')
    response = client.post('/api/projects/import', json={'path': str(tmp_path / 'missing')})
    assert response.status_code == 400
