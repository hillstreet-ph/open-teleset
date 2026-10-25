"""Guard the self-hosted vendor libraries.

The dashboard must load Vue, axios and supabase-js from this same origin
(/static/vendor/...), not only from third-party CDNs. Third-party CDNs
(unpkg/jsdelivr) are blocked on some mobile networks and in some regions,
which left the dashboard a silent blank page. These checks fail if a future
change drops the self-hosted copies or stops referencing them.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "static" / "vendor"
DASHBOARD = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")

VENDOR_FILES = {
    "vue.global.prod.js": 50_000,
    "axios.min.js": 20_000,
    "supabase.umd.js": 50_000,
}


def test_vendor_files_present_and_nontrivial():
    for name, min_size in VENDOR_FILES.items():
        path = VENDOR / name
        assert path.is_file(), f"missing self-hosted vendor lib: static/vendor/{name}"
        assert path.stat().st_size >= min_size, f"static/vendor/{name} looks truncated"


def test_dashboard_loads_vendor_from_same_origin():
    for name in VENDOR_FILES:
        assert f"/static/vendor/{name}" in DASHBOARD, (
            f"dashboard.html must load /static/vendor/{name} same-origin"
        )


def test_dashboard_uses_production_vue_build():
    # The dev build prints a console warning and is larger/slower; ship prod.
    assert "/static/vendor/vue.global.prod.js" in DASHBOARD
    assert '"https://unpkg.com/vue@3/dist/vue.global.js"' not in DASHBOARD
