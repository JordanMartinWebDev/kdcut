"""Channel stats: YouTube Analytics/Reporting parsing, the stats server Claude calls, and the
Claude desktop app hook-up."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kdcut import appcore, claude_link, mcp_server, stats

REACH_CSV = """date,channel_id,video_id,live_or_on_demand,subscribed_status,country_code,video_thumbnail_impressions,video_thumbnail_impressions_ctr
20261022,UC1,vidAAAAAAAA,on_demand,subscribed,US,1000,0.05
20261022,UC1,vidAAAAAAAA,on_demand,not_subscribed,US,3000,0.02
20261023,UC1,vidAAAAAAAA,on_demand,not_subscribed,GB,1000,0.01
20261023,UC1,vidBBBBBBBB,on_demand,not_subscribed,US,500,0.10
"""


class ReachTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(stats, "REACH_FILE", Path(self.tmp.name) / "reach.json")
        self.patch.start()
        mock.patch.object(stats, "STATS_DIR", Path(self.tmp.name)).start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def test_csv_rows_add_up_per_video_and_day(self):
        out = stats.parse_reach_csv(REACH_CSV)
        self.assertEqual(out["vidAAAAAAAA"]["20261022"], [4000.0, 110.0])
        self.assertEqual(out["vidBBBBBBBB"]["20261023"], [500.0, 50.0])

    def test_ctr_is_weighted_by_impressions(self):
        days = stats.parse_reach_csv(REACH_CSV)
        stats._save_reach({"job": "j", "done": [], "videos": days})
        r = stats.reach_by_video()
        self.assertEqual(r["vidAAAAAAAA"]["impressions"], 5000)
        self.assertEqual(r["vidAAAAAAAA"]["ctr"], "2.4%")       # (50 + 60 + 10) / 5000
        self.assertEqual(r["vidBBBBBBBB"]["ctr"], "10.0%")

    def test_sync_downloads_only_new_reports(self):
        calls = []

        def fake_get(url):
            calls.append(url)
            if url.endswith("/jobs"):
                return {"jobs": [{"id": "job1", "reportTypeId": stats.REACH_REPORT}]}
            return {"reports": [{"id": "r1", "downloadUrl": "https://dl/r1"}]}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(stats, "_get", fake_get), \
                mock.patch.object(stats.youtube, "_request", lambda *a, **k: Resp(REACH_CSV.encode())):
            self.assertEqual(stats.sync_reach(), 1)
            self.assertEqual(stats.sync_reach(), 0)              # r1 already read
        self.assertIn("vidAAAAAAAA", stats.reach_by_video())


class VideoTableTests(unittest.TestCase):
    def test_uploads_joined_with_analytics(self):
        def fake_get(url):
            if "/channels" in url:
                return {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}
            if "/playlistItems" in url:
                return {"items": [{"contentDetails": {"videoId": "longAAAAAAA"}},
                                  {"contentDetails": {"videoId": "shortBBBBBB"}},
                                  {"contentDetails": {"videoId": "privCCCCCCC"}}]}
            return {"items": [
                {"id": "longAAAAAAA", "snippet": {"title": "Survival Kingdom Ep 3", "publishedAt": "2026-10-24T15:00:00Z"},
                 "contentDetails": {"duration": "PT24M10S"}, "statistics": {"viewCount": "364"},
                 "status": {"privacyStatus": "public"}},
                {"id": "shortBBBBBB", "snippet": {"title": "My Starter House", "publishedAt": "2026-10-27T15:00:00Z"},
                 "contentDetails": {"duration": "PT24S"}, "statistics": {"viewCount": "1200"},
                 "status": {"privacyStatus": "public"}},
                {"id": "privCCCCCCC", "snippet": {"title": "Next week", "publishedAt": "2026-10-28T15:00:00Z"},
                 "contentDetails": {"duration": "PT20M"}, "statistics": {}, "status": {"privacyStatus": "private"}}]}

        rows = [{"video": "longAAAAAAA", "views": 364, "averageViewDuration": 655, "averageViewPercentage": 45.21,
                 "subscribersGained": 4}]
        with mock.patch.object(stats, "_get", fake_get), mock.patch.object(stats, "report", return_value=rows) as rep, \
                mock.patch.object(stats, "reach_by_video", return_value={"longAAAAAAA": {"ctr": "3.4%"}}):
            table = stats.video_table(10)
            self.assertIn("video==longAAAAAAA,shortBBBBBB", rep.call_args.kwargs["filters"])
            shorts_only = stats.video_table(10, "short")
        long = table[0]
        self.assertEqual((long["format"], long["length"], long["views"]), ("long", "24:10", 364))
        self.assertEqual(long["averageViewDuration"], "10:55")
        self.assertEqual(long["ctr"], "3.4%")
        self.assertEqual(table[1]["format"], "short")
        self.assertNotIn("views", table[2])                       # private: no analytics asked for
        self.assertEqual([v["title"] for v in shorts_only], ["My Starter House"])

    def test_durations(self):
        self.assertEqual(stats._seconds("PT1H2M3S"), 3723)
        self.assertEqual(stats._seconds("PT45S"), 45)
        self.assertEqual(stats._seconds(""), 0)

    def test_missing_permission_gets_a_plain_message(self):
        import urllib.error
        exc = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(
            b'{"error":{"message":"Request had insufficient authentication scopes."}}'))
        self.assertIn("Allow channel stats", str(stats._error(exc)))


class ServerTests(unittest.TestCase):
    def test_handshake_and_tool_list(self):
        init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({t["name"] for t in tools},
                         {"channel_overview", "recent_videos", "video_details", "traffic_sources"})

    def test_tool_errors_are_reported_to_claude_not_crashes(self):
        with mock.patch.object(stats, "overview", side_effect=stats.StatsError("Sign in first")):
            res = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                     "params": {"name": "channel_overview", "arguments": {"days": 7}}})
        self.assertTrue(res["result"]["isError"])
        self.assertIn("Sign in first", res["result"]["content"][0]["text"])

    def test_tool_result_is_json_text(self):
        with mock.patch.object(stats, "overview", return_value={"totals": {"views": 10}}) as ov:
            res = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                     "params": {"name": "channel_overview", "arguments": {"days": 7}}})
        ov.assert_called_once_with(7)
        self.assertEqual(json.loads(res["result"]["content"][0]["text"]), {"totals": {"views": 10}})

    def test_stdio_loop(self):
        lines = "\n".join(json.dumps(m) for m in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "nope"}]) + "\nnot json\n"
        out = io.StringIO()
        mcp_server.serve(io.StringIO(lines), out)
        replies = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([r.get("id") for r in replies], [1, 2, None])
        self.assertEqual(replies[1]["error"]["code"], -32601)
        self.assertEqual(replies[2]["error"]["code"], -32700)


class ClaudeLinkTests(unittest.TestCase):
    def test_adds_server_and_keeps_others(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "claude_desktop_config.json"
            cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
            entry = {"command": "C:/KDcut/kdcut-mcp.exe", "args": []}
            self.assertTrue(claude_link.add_server(cfg, entry))
            data = json.loads(cfg.read_text())
            self.assertEqual(data["mcpServers"]["other"], {"command": "x"})
            self.assertEqual(data["mcpServers"]["kdcut"], entry)
            self.assertEqual(data["theme"], "dark")
            self.assertTrue((Path(d) / "claude_desktop_config.json.backup").is_file())
            self.assertFalse(claude_link.add_server(cfg, entry))   # already there

    def test_broken_config_is_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "claude_desktop_config.json"
            cfg.write_text("{not json")
            with self.assertRaises(RuntimeError):
                claude_link.add_server(cfg, {"command": "x"})
            self.assertEqual(cfg.read_text(), "{not json")

    def test_new_config_file(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "Claude" / "claude_desktop_config.json"
            claude_link.add_server(cfg, {"command": "x", "args": []})
            self.assertEqual(json.loads(cfg.read_text()), {"mcpServers": {"kdcut": {"command": "x", "args": []}}})

    def test_source_install_runs_the_module(self):
        entry = claude_link.server_entry()
        self.assertEqual(entry["args"], ["-m", "kdcut.mcp_server"])


class WindowTests(unittest.TestCase):
    def test_geometry_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "window.json"
            self.assertEqual(appcore.load_window_geometry(f), "")
            appcore.save_window_geometry("AdnQywAD", f)
            self.assertEqual(appcore.load_window_geometry(f), "AdnQywAD")


if __name__ == "__main__":
    unittest.main()
