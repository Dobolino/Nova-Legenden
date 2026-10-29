"""Exercise the actual React editor against an isolated local API and SQLite DB."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

playwright = pytest.importorskip("playwright.sync_api")

from nova_legend.api.app import create_app
from test_phase2 import import_dxf, nova_like_dxf
from test_phase3 import env  # noqa: F401 - isolated settings and catalogue


def launch_chromium(pw):
    """Chromium for the browser tests: NOVA_TEST_CHROMIUM, Playwright's own, or one found
    on the machine. Without any browser the tests are skipped locally; in CI (GitHub
    Actions sets CI=true) they must run, so a missing browser fails there."""
    candidates = [os.environ.get("NOVA_TEST_CHROMIUM") or None]
    candidates += sorted(str(p) for p in Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"))
    error = None
    for path in candidates:
        try:
            return pw.chromium.launch(executable_path=path, headless=True)
        except Exception as exc:  # noqa: BLE001 - try the next browser
            error = exc
    if os.environ.get("CI"):
        raise RuntimeError(f"Kein Chromium für die Browser-Tests: {error}")
    pytest.skip("Kein Chromium für die Browser-Tests gefunden (NOVA_TEST_CHROMIUM setzen)")


@pytest.fixture()
def editor(env):
    client, tmp = env
    ui = Path(__file__).resolve().parents[2] / "ui" / "dist"
    if not (ui / "index.html").is_file():
        pytest.skip("Build the UI with npm run build before browser tests")
    ids = []
    for name in ("Browser A", "Browser B"):
        pid = client.post("/api/projects", json={"name": name}).json()["id"]
        assert import_dxf(client, pid, nova_like_dxf(tmp / f"{name}.dxf")).status_code == 200
        doc = client.post(f"/api/projects/{pid}/legend/propose", json={}).json()["doc"]
        doc["title"]["text"] = name
        assert client.put(f"/api/projects/{pid}/legend", json={"doc": doc}).status_code == 200
        ids.append(pid)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        create_app(client.app.state.nova, ui_dir=ui), log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("Browser test API did not start")
        time.sleep(0.02)
    try:
        with playwright.sync_playwright() as pw:
            browser = launch_chromium(pw)
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}")
            page.get_by_text("Browser A", exact=True).click()
            page.get_by_role("button", name="Legende", exact=True).click()
            page.get_by_role("button", name="T Titel", exact=True).click()
            field = page.get_by_label("Text", exact=True)
            playwright.expect(field).to_have_value("Browser A")
            yield page, field, client, ids
            assert not errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def title(client, pid):
    return client.get(f"/api/projects/{pid}/legend").json()["legend"]["doc"]["title"]["text"]


@pytest.mark.parametrize("destination", ["global_tab", "project_tab", "back", "project"])
def test_immediate_navigation_saves_to_the_original_project(editor, destination):
    page, field, client, (a, b) = editor
    # Reproduce the original cross-project bug with the destination's load delayed.
    page.evaluate("""() => {
        const original = window.fetch;
        window.fetch = async (...args) => {
            if (String(args[0]).includes('/Browser%20B/legend') && (!args[1]?.method || args[1].method === 'GET'))
                await new Promise(resolve => setTimeout(resolve, 1400));
            return original(...args);
        };
    }""")
    field.fill("Last edit in A")
    if destination == "global_tab":
        page.get_by_role("button", name="Kategorien", exact=True).click()
        playwright.expect(page.get_by_role("button", name="Kategorien", exact=True)).to_have_class("tab active")
    elif destination == "project_tab":
        page.get_by_role("button", name="Gesamtliste", exact=False).click()
        playwright.expect(field).not_to_be_visible()
    elif destination == "back":
        page.get_by_role("button", name="← Alle Projekte", exact=True).click()
        playwright.expect(page.get_by_role("button", name="Legende erstellen", exact=True)).to_be_visible()
    else:
        page.get_by_role("combobox", name="Projekt wechseln", exact=True).select_option(b)
        playwright.expect(page.get_by_role("heading", name="Browser B", exact=True)).to_be_visible()
    page.wait_for_timeout(1700)
    assert title(client, a) == "Last edit in A"
    assert title(client, b) == "Browser B"


@pytest.mark.parametrize("destination", ["global_tab", "project_tab", "project"])
def test_failed_save_keeps_the_editor_open_and_can_be_retried(editor, destination):
    page, field, client, (a, b) = editor

    def fail_save(route):
        if route.request.method == "PUT":
            route.fulfill(status=503, content_type="application/json", body='{"detail":"Speicher nicht erreichbar"}')
        else:
            route.continue_()

    page.route("**/legend", fail_save)
    field.fill("Retry this edit")
    if destination == "global_tab":
        page.get_by_role("button", name="Kategorien", exact=True).click()
    elif destination == "project_tab":
        page.get_by_role("button", name="Gesamtliste", exact=False).click()
    else:
        page.get_by_role("combobox", name="Projekt wechseln", exact=True).select_option(b)
    playwright.expect(page.get_by_text("Legende nicht gespeichert:", exact=False)).to_be_visible()
    playwright.expect(field).to_have_value("Retry this edit")
    playwright.expect(page.get_by_role("combobox", name="Projekt wechseln", exact=True)).to_have_value(a)
    assert title(client, a) == "Browser A"
    page.unroute("**/legend", fail_save)
    page.get_by_role("button", name="Kategorien", exact=True).click()
    playwright.expect(page.get_by_role("button", name="Kategorien", exact=True)).to_have_class("tab active")
    assert title(client, a) == "Retry this edit"


def test_navigation_waits_for_an_inflight_autosave_and_then_saves_the_latest_edit(editor):
    page, field, client, (a, b) = editor
    page.evaluate("""() => {
        const original = window.fetch;
        window.legendWrites = [];
        window.fetch = async (...args) => {
            if (String(args[0]).endsWith('/legend') && args[1]?.method === 'PUT') {
                window.legendWrites.push(JSON.parse(args[1].body).doc.title.text);
                if (window.legendWrites.length === 1) {
                    window.saveWaiting = true;
                    await new Promise(resolve => { window.releaseSave = resolve; });
                }
            }
            return original(...args);
        };
    }""")
    field.fill("First autosave")
    page.wait_for_function("window.saveWaiting === true")
    field.fill("Newest edit wins")
    page.evaluate("window.releaseSave()")
    page.get_by_role("combobox", name="Projekt wechseln", exact=True).select_option(b)
    playwright.expect(page.get_by_role("heading", name="Browser B", exact=True)).to_be_visible()
    assert title(client, a) == "Newest edit wins"
    assert title(client, b) == "Browser B"
    assert page.evaluate("window.legendWrites") == ["First autosave", "Newest edit wins"]


def test_copy_contains_the_edit_that_was_pending_when_copy_was_requested(editor):
    page, field, client, (a, b) = editor
    field.fill("Include me in the copy")
    page.once("dialog", lambda dialog: dialog.accept("Browser copy"))
    page.locator("summary").filter(has_text="Projektaktionen").click()
    page.get_by_role("button", name="Projekt duplizieren", exact=True).click()
    playwright.expect(page.get_by_role("heading", name="Browser copy", exact=True)).to_be_visible()
    assert title(client, a) == "Include me in the copy"
    assert title(client, "Browser copy") == "Include me in the copy"


def test_close_warning_only_appears_while_the_document_is_unsaved(editor):
    page, field, client, (a, b) = editor

    def closing_is_blocked():
        return page.evaluate("!window.dispatchEvent(new Event('beforeunload', { cancelable: true }))")

    assert not closing_is_blocked()
    field.fill("Not saved yet")
    assert closing_is_blocked()
    with page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/legend")):
        page.get_by_role("button", name="Kategorien", exact=True).click()
    playwright.expect(page.get_by_role("button", name="Kategorien", exact=True)).to_have_class("tab active")
    assert not closing_is_blocked()
    assert title(client, a) == "Not saved yet"


def edit_category(page):
    page.get_by_role("button", name="Kategorien", exact=True).click()
    page.locator(".cat-table tbody tr").first.click()
    field = page.get_by_label("Titel", exact=True)
    original = field.input_value()
    field.fill("Kategorie mit Änderungen")
    return field, original


@pytest.mark.parametrize("destination", ["workspace", "category"])
def test_category_navigation_can_cancel_or_discard_a_draft(editor, destination):
    page, _, client, _ = editor
    field, original = edit_category(page)

    def leave():
        if destination == "workspace":
            page.get_by_role("button", name="Projekte", exact=True).click()
        else:
            page.locator(".cat-table tbody tr").nth(1).click()

    assert page.evaluate("!window.dispatchEvent(new Event('beforeunload', { cancelable: true }))")
    leave()
    dialog = page.get_by_role("alertdialog", name="Änderungen speichern?")
    playwright.expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Abbrechen", exact=True).click()
    playwright.expect(field).to_have_value("Kategorie mit Änderungen")
    leave()
    dialog.get_by_role("button", name="Verwerfen", exact=True).click()
    playwright.expect(dialog).not_to_be_visible()
    if destination == "workspace":
        page.get_by_role("button", name="Kategorien", exact=True).click()
    page.locator(".cat-table tbody tr").first.click()
    playwright.expect(page.get_by_label("Titel", exact=True)).to_have_value(original)
    assert not page.evaluate("!window.dispatchEvent(new Event('beforeunload', { cancelable: true }))")
    assert client.get("/api/categories").json()["items"][0]["title"] == original


def test_category_navigation_saves_before_leaving_and_retries_a_failed_save(editor):
    page, _, client, _ = editor
    _, original = edit_category(page)

    def fail_save(route):
        if route.request.method == "PUT":
            route.fulfill(status=503, json={"detail": "Firmenordner nicht erreichbar"})
        else:
            route.continue_()

    page.route("**/api/categories/*", fail_save)
    page.get_by_role("button", name="Projekte", exact=True).click()
    dialog = page.get_by_role("alertdialog", name="Änderungen speichern?")
    dialog.get_by_role("button", name="Speichern", exact=True).click()
    playwright.expect(dialog.get_by_role("alert")).to_contain_text("nicht gespeichert")
    assert client.get("/api/categories").json()["items"][0]["title"] == original
    page.unroute("**/api/categories/*", fail_save)
    dialog.get_by_role("button", name="Speichern", exact=True).click()
    playwright.expect(dialog).not_to_be_visible()
    playwright.expect(page.get_by_role("button", name="Projekte", exact=True)).to_have_class("tab active")
    assert client.get("/api/categories").json()["items"][0]["title"] == "Kategorie mit Änderungen"
    assert not page.evaluate("!window.dispatchEvent(new Event('beforeunload', { cancelable: true }))")


def test_invalid_category_cannot_be_saved_by_navigation(editor):
    page, _, client, _ = editor
    _, original = edit_category(page)
    page.get_by_label("Spalten in der Legende", exact=True).fill("0")
    page.get_by_role("button", name="Projekte", exact=True).click()
    dialog = page.get_by_role("alertdialog", name="Änderungen speichern?")
    dialog.get_by_role("button", name="Speichern", exact=True).click()
    playwright.expect(dialog.get_by_role("alert")).to_be_visible()
    dialog.get_by_role("button", name="Abbrechen", exact=True).click()
    playwright.expect(page.get_by_label("Titel", exact=True)).to_have_value("Kategorie mit Änderungen")
    assert client.get("/api/categories").json()["items"][0]["title"] == original


@pytest.mark.parametrize("old_response", ["success", "error"])
def test_library_ignores_an_outdated_search_response(editor, old_response):
    page, _, _, _ = editor
    page.get_by_role("button", name="Bibliothek", exact=True).click()
    playwright.expect(page.locator(".tile").first).to_be_visible()
    page.evaluate("""(oldResponse) => {
        const original = window.fetch;
        window.fetch = async (...args) => {
            const response = await original(...args);
            const url = new URL(String(args[0]), location.href);
            if (url.pathname === '/api/library/families' && url.searchParams.get('q') === 'Schalter') {
                window.oldSearchWaiting = true;
                await new Promise(resolve => { window.releaseOldSearch = resolve; });
                if (oldResponse === 'error') throw new Error('Outdated search failed');
            }
            return response;
        };
    }""", old_response)
    search = page.get_by_role("textbox", name="Symbole suchen")
    search.fill("Schalter")
    page.wait_for_function("window.oldSearchWaiting === true")
    search.fill("Brandmelder")
    playwright.expect(page.locator(".tile .name")).to_have_text(["Brandmelder"])
    page.evaluate("async () => { window.releaseOldSearch(); await new Promise(resolve => setTimeout(resolve, 100)); }")
    playwright.expect(search).to_have_value("Brandmelder")
    playwright.expect(page.locator(".tile .name")).to_have_text(["Brandmelder"])
    playwright.expect(page.locator(".toast.error")).not_to_be_visible()
    playwright.expect(page.locator(".result-count")).to_have_text("1 Eintrag")


@pytest.mark.parametrize("override", [None, "19.2"])
def test_new_project_uses_the_configured_nova_version_and_allows_an_override(editor, override):
    page, _, client, _ = editor
    assert client.put("/api/settings", json={"nova_version": "20"}).status_code == 200
    page.reload()
    page.get_by_role("button", name="Legende erstellen", exact=True).click()
    page.get_by_role("radio", name="Neues Projekt", exact=True).check()
    version = page.get_by_label("Nova-Version", exact=True)
    playwright.expect(version).to_have_value("20")
    if override:
        version.select_option(override)
    page.get_by_label("Bezeichnung", exact=True).fill("Nova version test")
    page.get_by_role("button", name="Projekt anlegen", exact=True).click()
    playwright.expect(page.get_by_role("heading", name="Nova version test", exact=True)).to_be_visible()
    assert client.get("/api/projects/Nova%20version%20test").json()["meta"]["nova_version"] == (override or "20")


def test_editor_space_and_view_options_do_not_change_the_exported_document(editor):
    page, _, client, (a, _) = editor
    page.set_viewport_size({"width": 1440, "height": 960})
    page.locator(".project-page").evaluate("node => { node.scrollTop = 0; }")
    assert not page.locator("details.fold").evaluate("node => node.open")
    canvas = page.locator(".legend-canvas").bounding_box()
    assert canvas["y"] < 500
    assert min(canvas["height"], 960 - canvas["y"]) >= 400
    before = client.get(f"/api/projects/{a}/legend").json()["legend"]["doc"]
    page.locator("summary").filter(has_text="Ansicht").click()
    playwright.expect(page.get_by_role("group", name="Ansicht", exact=True).get_by_role("combobox", name="Rastermass")).to_be_focused()
    page.get_by_role("button", name="Raster anzeigen", exact=True).click()
    page.get_by_role("button", name="Dunkler Modus", exact=True).click()
    assert page.locator(".legend-paper > rect").first.evaluate("node => getComputedStyle(node).fill") == "rgb(255, 255, 255)"
    page.locator("summary").filter(has_text="Ansicht").click()
    page.get_by_role("button", name="Als Fenster", exact=True).click()
    playwright.expect(page.locator(".legend-editor.windowed")).to_be_visible()
    page.locator("summary").filter(has_text="Ansicht").click()
    view_menu = page.get_by_role("group", name="Ansicht", exact=True)
    playwright.expect(view_menu.get_by_role("combobox", name="Rastermass")).to_be_visible()
    menu_box = view_menu.bounding_box()
    assert menu_box["y"] >= 0 and menu_box["y"] + menu_box["height"] <= 960
    page.keyboard.press("Escape")
    playwright.expect(view_menu).not_to_be_visible()
    playwright.expect(page.locator("summary").filter(has_text="Ansicht")).to_be_focused()
    playwright.expect(page.locator(".legend-editor.windowed")).to_be_visible()
    page.get_by_role("button", name="Fenster schliessen", exact=True).click()
    playwright.expect(page.locator(".legend-editor.windowed")).not_to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("summary").filter(has_text="Ansicht").click()
    playwright.expect(view_menu).to_be_visible()
    menu_box = view_menu.bounding_box()
    assert menu_box["x"] >= 0 and menu_box["x"] + menu_box["width"] <= 390
    page.keyboard.press("Shift+Tab")
    playwright.expect(view_menu).not_to_be_visible()
    playwright.expect(page.locator("summary").filter(has_text="Ansicht")).to_be_focused()
    page.set_viewport_size({"width": 1440, "height": 960})
    with page.expect_download() as downloaded:
        page.get_by_role("button", name="DXF herunterladen", exact=True).click()
    import ezdxf
    assert len(ezdxf.readfile(downloaded.value.path()).modelspace()) > 0
    assert client.get(f"/api/projects/{a}/legend").json()["legend"]["doc"] == before
