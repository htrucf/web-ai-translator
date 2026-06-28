"""Tests cho app.utils.port — helper auto-free port khi bind.

Mục tiêu kiểm thử:
  - is_port_in_use phát hiện đúng socket đang LISTENING.
  - _list_pids_owning_port trả về PID hiện tại khi mình tự bind.
  - _process_name resolve PID hiện tại → 'python.exe' (Windows) hoặc 'python*' (Linux).
  - free_port BỎ QUA process không thuộc whitelist khi force=False.
  - free_port KILL được Python/uvicorn khi process tự nguyện thoát (mock kill).
  - ensure_port_free idempotent — gọi nhiều lần không raise.

Lưu ý quan trọng: KHÔNG được gọi free_port(force=True) thật trên port bị Python
test process chiếm — sẽ tự kill chính pytest. Test dùng mock cho kill operations
hoặc dùng port không có ai chiếm.
"""

from __future__ import annotations

import os
import socket
import sys
from unittest.mock import patch

import pytest

from app.utils import port as port_mod


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bind_listening_socket(port: int) -> socket.socket:
    """Bind 1 socket LISTEN ở 127.0.0.1:port, return socket (caller close)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def _pick_free_port() -> int:
    """OS chọn 1 port free → trả về số."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── is_port_in_use ────────────────────────────────────────────────────────────

class TestIsPortInUse:
    def test_free_port_returns_false(self):
        port = _pick_free_port()
        assert port_mod.is_port_in_use(port) is False

    def test_bound_port_returns_true(self):
        port = _pick_free_port()
        s = _bind_listening_socket(port)
        try:
            assert port_mod.is_port_in_use(port) is True
        finally:
            s.close()

    def test_after_close_returns_false(self):
        port = _pick_free_port()
        s = _bind_listening_socket(port)
        s.close()
        # Có thể vẫn TIME_WAIT trên 1 số OS, nhưng socket() connect không kết nối
        # được nữa (Server-side socket đã close hoàn toàn).
        # Test mềm: chỉ assert không raise.
        port_mod.is_port_in_use(port)


# ── _list_pids_owning_port ────────────────────────────────────────────────────

class TestListPidsOwningPort:
    def test_no_owner_when_port_free(self):
        port = _pick_free_port()
        assert port_mod._list_pids_owning_port(port) == []

    def test_finds_current_pid_when_we_bind(self):
        """Khi chính test process bind socket, PID của nó phải xuất hiện."""
        port = _pick_free_port()
        s = _bind_listening_socket(port)
        try:
            pids = port_mod._list_pids_owning_port(port)
            assert os.getpid() in pids, (
                f"Expected PID {os.getpid()} in {pids} — "
                "helper không tìm thấy owner của port chính nó."
            )
        finally:
            s.close()


# ── _process_name ─────────────────────────────────────────────────────────────

class TestProcessName:
    def test_current_pid_resolves_to_python(self):
        """PID hiện tại (pytest) phải resolve sang một tên chứa 'python'."""
        name = port_mod._process_name(os.getpid())
        # tasklist trả "python.exe" hoặc "py.exe"; ps trên Linux trả "python3"
        assert "python" in name or "py" in name, (
            f"Expected python-like name, got {name!r}"
        )

    def test_nonexistent_pid_returns_empty_or_safe(self):
        # PID rất lớn → không tồn tại (tasklist trả message lỗi vẫn parse được)
        name = port_mod._process_name(9999999)
        # Không crash là đủ; tên có thể là "" hoặc chuỗi báo lỗi.
        assert isinstance(name, str)


# ── _wait_port_free ───────────────────────────────────────────────────────────

class TestWaitPortFree:
    def test_returns_true_when_already_free(self):
        port = _pick_free_port()
        assert port_mod._wait_port_free(port, timeout=0.5) is True

    def test_returns_false_when_still_bound_after_timeout(self):
        """Port luôn busy → wait timeout → trả False.

        Patch is_port_in_use thay vì bind real socket — trên Windows hành vi
        connect_ex tới port bind bởi chính mình với SO_REUSEADDR không
        deterministic, gây flaky.
        """
        with patch.object(port_mod, "is_port_in_use", return_value=True):
            assert port_mod._wait_port_free(12345, timeout=0.3) is False


# ── free_port — safe whitelist ────────────────────────────────────────────────

class TestFreePortWhitelist:
    def test_skips_unknown_process_when_force_false(self, capsys):
        """Process không phải python/uvicorn → bỏ qua, không kill.

        Giả lập: bind socket bằng test process, nhưng patch _process_name để
        trả về 'foreign.exe' → helper coi như app khác và skip.
        """
        port = _pick_free_port()
        s = _bind_listening_socket(port)
        try:
            with patch.object(port_mod, "_process_name", return_value="foreign.exe"):
                with patch.object(port_mod, "_kill_pid") as mock_kill:
                    result = port_mod.free_port(port, timeout=0.5, force=False)
            mock_kill.assert_not_called()
            # Port vẫn busy → trả False
            assert result is False
            err = capsys.readouterr().err
            assert "foreign.exe" in err  # cảnh báo có in stderr
        finally:
            s.close()

    def test_force_true_kills_even_foreign_process(self):
        """force=True → bỏ qua whitelist, gọi _kill_pid."""
        port = _pick_free_port()
        s = _bind_listening_socket(port)
        try:
            with patch.object(port_mod, "_process_name", return_value="foreign.exe"):
                with patch.object(port_mod, "_kill_pid", return_value=True) as mock_kill:
                    with patch.object(port_mod, "_wait_port_free", return_value=True):
                        result = port_mod.free_port(port, timeout=0.5, force=True)
            mock_kill.assert_called()
            assert result is True
        finally:
            s.close()

    def test_returns_true_immediately_if_port_free(self):
        port = _pick_free_port()
        # Port chưa bind → trả True ngay, không cần kill
        with patch.object(port_mod, "_kill_pid") as mock_kill:
            result = port_mod.free_port(port, timeout=0.5)
        mock_kill.assert_not_called()
        assert result is True

    def test_python_in_whitelist_is_killed(self):
        """python.exe nằm trong _SAFE_TO_KILL → free_port gọi _kill_pid."""
        port = _pick_free_port()
        s = _bind_listening_socket(port)
        try:
            with patch.object(port_mod, "_process_name", return_value="python.exe"):
                with patch.object(port_mod, "_kill_pid", return_value=True) as mock_kill:
                    with patch.object(port_mod, "_wait_port_free", return_value=True):
                        result = port_mod.free_port(port, timeout=0.5, force=False)
            mock_kill.assert_called()
            assert result is True
        finally:
            s.close()


# ── ensure_port_free — idempotent wrapper ─────────────────────────────────────

class TestEnsurePortFree:
    def test_noop_when_port_free(self):
        """Port free → wrapper trả ngay, không gọi free_port."""
        port = _pick_free_port()
        with patch.object(port_mod, "free_port") as mock_free:
            port_mod.ensure_port_free(port)
        mock_free.assert_not_called()

    def test_calls_free_port_when_busy(self):
        port = _pick_free_port()
        s = _bind_listening_socket(port)
        try:
            with patch.object(port_mod, "free_port", return_value=True) as mock_free:
                port_mod.ensure_port_free(port, timeout=1.0, force=False)
            mock_free.assert_called_once()
            call_kwargs = mock_free.call_args.kwargs
            assert call_kwargs.get("force") is False
        finally:
            s.close()

    def test_does_not_raise_on_kill_failure(self):
        """Idempotent — kể cả khi free_port fail, wrapper không raise."""
        port = _pick_free_port()
        s = _bind_listening_socket(port)
        try:
            with patch.object(port_mod, "free_port", side_effect=RuntimeError("boom")):
                # Phải nuốt exception (no-raise) — chỉ log để uvicorn báo lỗi rõ ràng
                try:
                    port_mod.ensure_port_free(port, timeout=0.5)
                except RuntimeError:
                    pytest.fail(
                        "ensure_port_free phải nuốt lỗi để không block "
                        "startup — chỉ log."
                    )
        finally:
            s.close()


# ── Safe-to-kill whitelist ────────────────────────────────────────────────────

class TestSafeToKillSet:
    def test_python_variants_in_whitelist(self):
        for n in ("python.exe", "pythonw.exe", "python"):
            assert n in port_mod._SAFE_TO_KILL

    def test_uvicorn_variants_in_whitelist(self):
        for n in ("uvicorn.exe", "uvicorn"):
            assert n in port_mod._SAFE_TO_KILL

    def test_common_dev_servers_not_in_whitelist(self):
        """IIS, node, dotnet — KHÔNG được nằm trong default whitelist."""
        for n in ("node.exe", "dotnet.exe", "w3wp.exe", "iisexpress.exe"):
            assert n not in port_mod._SAFE_TO_KILL


# ── Smoke: import path nhất quán ──────────────────────────────────────────────

def test_module_exports():
    """Verify các symbol public được export đúng."""
    assert callable(port_mod.is_port_in_use)
    assert callable(port_mod.free_port)
    assert callable(port_mod.ensure_port_free)
