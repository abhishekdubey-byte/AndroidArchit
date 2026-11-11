"""Phase 4: the phone's AAC microphone through the hub (camera sound, Listen Live, recordings)."""
import unittest

from app.services.media_packets import KIND_AUDIO, KIND_AUDIO_CONFIG, unpack
from tests.test_phase3_fixes import frame, make_hub, start, viewer

ASC = b"\x11\x88"


def audio(k, pts, data):
    return frame({"type": "CAMERA_PKT", "k": k, "pts": pts}, data)


def phone_sent(phone):
    return [m["type"] for m in phone.json]


async def listen(hub, ws, on=True):
    await hub._process_dashboard_message(ws, {"type": "START_PHONE_AUDIO" if on else "STOP_PHONE_AUDIO",
                                              "device_id": "ph", "payload": {}})


def new_app(hub):
    hub._phone_media["rid"] = {"aac", "screen_h264"}


class TestHubAudio(unittest.IsolatedAsyncioTestCase):
    async def test_camera_viewer_hears_the_phone(self):
        hub, phone = make_hub()
        ws = await viewer(hub)
        await start(hub, ws)
        await hub.handle_device_binary(phone, audio(4, 10, ASC))
        await hub.handle_device_binary(phone, audio(5, 20, b"AAC"))
        await hub.fanout.flush()
        got = [unpack(b) for b in ws.binary]
        self.assertEqual([(k, s) for k, s, _, _ in got], [(KIND_AUDIO_CONFIG, "ph#mic"), (KIND_AUDIO, "ph#mic")])
        self.assertEqual(hub._mic_config["ph"], ASC)

    async def test_recording_gets_aac_packets_and_cached_config(self):
        hub, phone = make_hub()
        ws = await viewer(hub)
        await start(hub, ws)
        await hub.handle_device_binary(phone, audio(4, 10, ASC))
        made = {}

        class Rec:
            def __init__(self, *a, **kw):
                made.update(kw)
                self.packets = []
                made["rec"] = self

            def submit_audio_packet(self, k, pts, data):
                self.packets.append((k, pts))

        import app.websocket.hub as hub_module
        import app.services.cam_record as cam_record
        orig, orig_ff = hub_module.AutoRecorder, cam_record.FFMPEG
        hub_module.AutoRecorder, cam_record.FFMPEG = Rec, cam_record.FFMPEG or "ffmpeg"
        try:
            await hub._process_dashboard_message(ws, {"type": "START_CAMERA_SAVE", "device_id": "ph"})
        finally:
            hub_module.AutoRecorder, cam_record.FFMPEG = orig, orig_ff
        self.assertEqual(made["audio_config"], ASC)
        self.assertFalse(made["legacy_audio"])
        await hub.handle_device_binary(phone, audio(5, 30, b"AAC"))
        self.assertEqual(made["rec"].packets, [(5, 30)])

    async def test_old_app_pcm_marks_recording_legacy(self):
        hub, phone = make_hub()
        await hub._process_dashboard_message(phone, {"type": "CAMERA_AUDIO", "device_id": "rid",
                                                     "payload": {"pcm_base64": "AAAA", "sample_rate": 16000}})
        self.assertIn("ph", hub._legacy_pcm)
        await hub.handle_device_binary(phone, audio(4, 10, ASC))     # updated app: AAC again
        self.assertNotIn("ph", hub._legacy_pcm)

    async def test_listen_live_starts_once_and_stops_with_the_last_listener(self):
        hub, phone = make_hub()
        new_app(hub)
        a, b = await viewer(hub), await viewer(hub)
        await listen(hub, a)
        await listen(hub, b)
        self.assertEqual(phone_sent(phone).count("START_PHONE_AUDIO"), 1)
        await hub.handle_device_binary(phone, audio(4, 10, ASC))
        await hub.handle_device_binary(phone, audio(5, 20, b"AAC"))
        await hub.fanout.flush()
        self.assertTrue(a.binary and b.binary)
        await listen(hub, a, on=False)
        self.assertNotIn("STOP_PHONE_AUDIO", phone_sent(phone))
        await hub.dashboard_left(b)                                 # closing the tab stops the phone's mic
        self.assertIn("STOP_PHONE_AUDIO", phone_sent(phone))
        self.assertNotIn("ph", hub._mic_config)

    async def test_listen_live_refused_for_old_app(self):
        hub, phone = make_hub()
        ws = await viewer(hub)
        await listen(hub, ws)
        self.assertNotIn("START_PHONE_AUDIO", phone_sent(phone))
        state = [m for m in ws.json if m["type"] == "PHONE_AUDIO_STATE"][-1]["payload"]
        self.assertFalse(state["active"])
        self.assertIn("1.9.29", state["error"])

    async def test_listen_live_needs_webcodecs(self):
        hub, phone = make_hub()
        new_app(hub)
        ws = await viewer(hub, h264=False)
        await listen(hub, ws)
        self.assertNotIn("START_PHONE_AUDIO", phone_sent(phone))

    async def test_phone_mic_failure_reaches_dashboards_and_clears_listeners(self):
        hub, phone = make_hub()
        new_app(hub)
        ws = await viewer(hub)
        await listen(hub, ws)
        await hub._process_dashboard_message(phone, {"type": "PHONE_AUDIO_STATE", "device_id": "rid",
                                                     "payload": {"active": False, "error": "Microphone is busy"}})
        self.assertNotIn("ph", hub._listeners)
        self.assertTrue(any(m["type"] == "PHONE_AUDIO_STATE" and m["payload"].get("error") for m in ws.json))

    async def test_mic_kept_while_listening_after_camera_stops(self):
        hub, phone = make_hub()
        new_app(hub)
        ws = await viewer(hub)
        await start(hub, ws)
        await listen(hub, ws)
        await hub.handle_device_binary(phone, audio(4, 10, ASC))
        await hub._process_dashboard_message(ws, {"type": "STOP_CAMERA_STREAM", "device_id": "ph"})
        self.assertEqual(hub._mic_config.get("ph"), ASC)
        self.assertIn(ws, hub.fanout.subscriber_sockets("ph#mic"))


if __name__ == "__main__":
    unittest.main()
