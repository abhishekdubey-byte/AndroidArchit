import asyncio
import json
import base64
import websockets
import urllib.request

async def test_full_call_flow():
    uri = "ws://localhost:8000/ws/dashboard"
    print(f"[Test] Connecting to {uri}...")
    
    async with websockets.connect(uri) as ws:
        # 1. Receive initial state
        init_raw = await ws.recv()
        init_msg = json.loads(init_raw)
        assert init_msg["type"] == "INITIAL_STATE"
        devices = init_msg["data"]["devices"]
        print(f"[Test] Received INITIAL_STATE with {len(devices)} device(s):")
        for d in devices:
            print(f"  - {d['id']} ({d['name']})")
        
        target_dev_id = devices[0]["id"]
        
        # 2. Trigger an incoming call simulation via REST
        sim_url = f"http://localhost:8000/api/devices/{target_dev_id}/call/simulate_incoming?caller_name=Bro+Code&phone_number=%2B918269220295"
        print(f"[Test] Triggering incoming call simulation via REST: {sim_url}")
        req = urllib.request.Request(sim_url, method="POST")
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            print("[Test] REST call simulation triggered successfully")

        # 3. Listen for INCOMING_CALL on WebSocket
        incoming_event = None
        for _ in range(5):
            raw = await asyncio.wait_for(ws.recv(), timeout=4.0)
            msg = json.loads(raw)
            if msg.get("type") == "INCOMING_CALL":
                incoming_event = msg
                break

        assert incoming_event is not None, "Did not receive INCOMING_CALL event"
        print(f"[Test] SUCCESS: Received INCOMING_CALL:")
        print(f"  Caller: {incoming_event['payload'].get('caller_name')} ({incoming_event['payload'].get('phone_number')})")

        # 4. Accept Call from Laptop via WebSocket
        print("[Test] Sending ACCEPT_CALL from laptop...")
        await ws.send(json.dumps({
            "type": "ACCEPT_CALL",
            "device_id": target_dev_id
        }))

        # 5. Listen for CALL_ACTIVE event
        active_event = None
        for _ in range(5):
            raw = await asyncio.wait_for(ws.recv(), timeout=4.0)
            msg = json.loads(raw)
            if msg.get("type") == "CALL_ACTIVE":
                active_event = msg
                break

        assert active_event is not None, "Did not receive CALL_ACTIVE event"
        print(f"[Test] SUCCESS: Call answered! State is ACTIVE.")

        # 6. Stream Laptop Mic Audio chunk to Caller
        print("[Test] Streaming laptop microphone audio chunk (16kHz PCM)...")
        dummy_pcm = bytes([0, 10, 0, 20, 0, 30] * 100)
        pcm_b64 = base64.b64encode(dummy_pcm).decode('ascii')
        await ws.send(json.dumps({
            "type": "LAPTOP_MIC_AUDIO",
            "device_id": target_dev_id,
            "payload": {
                "pcm_base64": pcm_b64
            }
        }))
        print("[Test] SUCCESS: Laptop mic audio chunk transmitted.")

        # 7. End Call from Laptop via WebSocket
        print("[Test] Sending END_CALL from laptop...")
        await ws.send(json.dumps({
            "type": "END_CALL",
            "device_id": target_dev_id
        }))

        # 8. Listen for CALL_ENDED event
        ended_event = None
        for _ in range(5):
            raw = await asyncio.wait_for(ws.recv(), timeout=4.0)
            msg = json.loads(raw)
            if msg.get("type") == "CALL_ENDED":
                ended_event = msg
                break

        assert ended_event is not None, "Did not receive CALL_ENDED event"
        print("[Test] SUCCESS: Received CALL_ENDED! Call terminated cleanly.")
        print("\n>>> ALL INCOMING CALL & AUDIO TESTS PASSED! <<<")

if __name__ == "__main__":
    asyncio.run(test_full_call_flow())
