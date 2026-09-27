"""Apply the Chromium 152 touch-emulation compatibility fix to Computer 0.9.21."""

from pathlib import Path

import cptr.utils.browser.viewer as viewer
import cptr.utils.browser.proxy as browser_proxy
import cptr.routers.browser as browser_router


def main() -> None:
    viewer_path = Path(viewer.__file__)
    source = viewer_path.read_text(encoding="utf-8")
    old = '"maxTouchPoints": profile["max_touch_points"],'
    new = '"maxTouchPoints": max(1, int(profile["max_touch_points"] or 0)),'
    if source.count(old) != 1:
        raise RuntimeError(f"Unexpected Computer viewer source: {viewer_path}")
    source = source.replace(old, new)
    focus_old = '            if viewer.personal:\n                await self._focus_personal(viewer, peer)\n            return'
    focus_new = '            if viewer.personal:\n                await self._focus_personal(viewer, peer)\n            else:\n                await self._claim_managed_control(viewer, peer)\n            return'
    visibility_old = '        peer.visible = visible\n        if viewer.personal:'
    visibility_new = '        peer.visible = visible\n        if visible and not viewer.personal:\n            await self._claim_managed_control(viewer, peer)\n        if viewer.personal:'
    helper = '''    async def _claim_managed_control(self, viewer, peer) -> None:
        if viewer.personal or peer not in viewer.peers or viewer.controller is peer:
            return
        previous = viewer.controller
        viewer.controller = peer
        for target, enabled in ((previous, False), (peer, True)):
            if target is not None:
                with contextlib.suppress(asyncio.QueueFull):
                    target.queue.put_nowait(
                        {"type": "ready", "mode": "chrome", "controller": enabled,
                         "managed": True}
                    )

'''
    method_marker = '    async def _set_visibility('
    for before, after in ((focus_old, focus_new), (visibility_old, visibility_new),
                          (method_marker, helper + method_marker)):
        if source.count(before) != 1:
            raise RuntimeError("Unexpected Computer control ownership implementation")
        source = source.replace(before, after)
    viewer_path.write_text(source, encoding="utf-8")

    proxy_path = Path(browser_proxy.__file__)
    proxy_source = proxy_path.read_text(encoding="utf-8")
    close_method = """    async def close(self, session_id: str, owner: str) -> bool:
"""
    restore_method = """    async def restore(
        self, session_id: str, owner: str, *, url: str = ""
    ) -> BrowserSession:
        \"\"\"Restore one authenticated UI tab after a service restart.\"\"\"
        async with self._lock:
            existing = self.session(session_id, owner)
            if existing is not None:
                return existing
            session = BrowserSession(session_id, owner, url=url)
            if url:
                parsed = urlsplit(url)
                session.origin = (
                    f"{parsed.scheme}://{parsed.netloc}"
                    if parsed.scheme and parsed.netloc
                    else ""
                )
            self._sessions[session_id] = session
            self._profile(owner)
            return session

"""
    if proxy_source.count(close_method) != 1:
        raise RuntimeError(f"Unexpected Computer browser proxy source: {proxy_path}")
    proxy_path.write_text(
        proxy_source.replace(close_method, restore_method + close_method),
        encoding="utf-8",
    )

    router_path = Path(browser_router.__file__)
    router_source = router_path.read_text(encoding="utf-8")
    initial_url = (
        "    initial_url = _initial_url(payload.get(\"url\") "
        "if isinstance(payload, dict) else None)"
    )
    google_sites_default = (
        initial_url
        + "\n    if not initial_url:"
        + "\n        initial_url = \"https://sites.google.com/new\""
    )
    if router_source.count(initial_url) != 1:
        raise RuntimeError(f"Unexpected Computer browser router source: {router_path}")
    router_source = router_source.replace(initial_url, google_sites_default)
    logger_marker = "logger = logging.getLogger(__name__)\n"
    recovery_state = logger_marker + "\n_stale_session_recovery_attempted: set[str] = set()\n"
    if router_source.count(logger_marker) != 1:
        raise RuntimeError(f"Unexpected Computer browser router logger: {router_path}")
    router_source = router_source.replace(logger_marker, recovery_state)
    stale_stream = """    session = manager.session(session_id, owner) if auth else None
    viewer = chrome_viewer_manager.viewer_for(session_id)
    if not session or session.mode != "chrome" or not viewer:
"""
    recovered_stream = """    session = manager.session(session_id, owner) if auth else None
    if auth and session is None and session_id not in _stale_session_recovery_attempted:
        _stale_session_recovery_attempted.add(session_id)
        try:
            session = await manager.restore(
                session_id, owner, url="https://sites.google.com/new"
            )
            session.mode = "chrome"
            session.status = "connecting"
            await chrome_viewer_manager.start(
                session, local_origin(str(websocket.base_url))
            )
            logger.info("Recovered stale authenticated Browser tab %s", session_id)
        except Exception as exc:
            logger.warning("Stale Browser tab recovery failed for %s: %s", session_id, exc)
            await manager.close(session_id, owner)
            session = None
    viewer = chrome_viewer_manager.viewer_for(session_id)
    if not session or session.mode != "chrome" or not viewer:
"""
    if router_source.count(stale_stream) != 1:
        raise RuntimeError(f"Unexpected Computer browser stream route: {router_path}")
    router_path.write_text(
        router_source.replace(stale_stream, recovered_stream), encoding="utf-8"
    )

    frontend_nodes = viewer_path.parents[2] / "frontend" / "build" / "_app" / "immutable" / "nodes"
    deep_link_candidates = [
        path for path in frontend_nodes.glob("*.js")
        if "case`newTerminal`" in path.read_text(encoding="utf-8")
        and "/api/browser/sessions" in path.read_text(encoding="utf-8")
    ]
    if len(deep_link_candidates) != 1:
        raise RuntimeError("Unexpected Computer home frontend for Browser deep link")
    frontend_path = deep_link_candidates[0]
    frontend_source = frontend_path.read_text(encoding="utf-8")
    intent_old = (
        ':r===`newTerminal`?{kind:`newTerminal`,workspace:i}'
        ':r===`openWorkspace`'
    )
    intent_new = (
        ':r===`newTerminal`?{kind:`newTerminal`,workspace:i}'
        ':r===`newBrowser`?{kind:`newBrowser`,url:n.get(`url`)||void 0}'
        ':r===`openWorkspace`'
    )
    dispatch_old = (
        'case`newTerminal`:t?await Ue():await oe();break;'
        'case`search`:'
    )
    dispatch_new = (
        'case`newTerminal`:t?await Ue():await oe();break;'
        'case`newBrowser`:await se(e.url);break;'
        'case`search`:'
    )
    for before, after in ((intent_old, intent_new), (dispatch_old, dispatch_new)):
        if frontend_source.count(before) != 1:
            raise RuntimeError("Unexpected Computer Browser deep-link implementation")
        frontend_source = frontend_source.replace(before, after)
    frontend_path.write_text(frontend_source, encoding="utf-8")


if __name__ == "__main__":
    main()
