package com.operator.companion.hunt

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Matrix
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.util.Base64
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.animation.core.*
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import com.operator.companion.network.CompanionWebSocketClient
import kotlinx.coroutines.delay
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.Executors
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.sin
import kotlin.random.Random

private val Cyan = Color(0xFF22D3EE)
private val Violet = Color(0xFFA855F7)
private val Gold = Color(0xFFFACC15)
private val Panel = Color(0xCC0B1024)

/** The one creature loose right now: where it is, how far, and what it's doing. */
private enum class Mood { ROAM, FLEE, ATTACK, REST, LOST }
private class Wild(val sp: Species, var bearing: Float, var elev: Float, var dist: Float, val seed: Float) {
    var mood = Mood.ROAM
    var moodUntil = 0f
    var seenFor = 0f          // seconds continuously on screen
    var offFor = 0f           // seconds continuously off screen
    var noticed = false
    var facingAway = false
    var coreTaps = 0
    var openUntil = 0f        // weakness exposed until this time (seconds)
    var fleeDir = 1f
}

private const val H_FOV = 62f   // approx. camera field of view (portrait), degrees
private const val V_FOV = 90f
private const val RANGE = 6f    // metres: DNA can be taken inside this distance

private fun wrap(d: Float): Float { var x = d % 360f; if (x > 180f) x -= 360f; if (x < -180f) x += 360f; return x }

@Composable
fun HuntScreen(onExit: () -> Unit, onBestiary: () -> Unit) {
    val ctx = LocalContext.current
    var camOk by remember { mutableStateOf(ContextCompat.checkSelfPermission(ctx, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) }
    val ask = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { camOk = it }
    BackHandler(onBack = onExit)
    if (!camOk) {
        Column(Modifier.fillMaxSize().background(Color(0xFF07090E)).padding(28.dp), verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally) {
            Text("📷", fontSize = 54.sp)
            Text("The HUNT needs the camera", color = Color.White, fontSize = 20.sp, fontWeight = FontWeight.Black)
            Spacer(Modifier.height(18.dp))
            HudButton("ALLOW CAMERA", Cyan) { ask.launch(Manifest.permission.CAMERA) }
            Spacer(Modifier.height(10.dp))
            Text("← Back to the mines", color = Color(0xFF94A3B8), modifier = Modifier.clickable(onClick = onExit).padding(10.dp))
        }
        return
    }
    // The hunt is a portrait camera game: rotating the phone must only look around, never rotate/restart the screen
    DisposableEffect(Unit) {
        val act = ctx as? android.app.Activity
        val before = act?.requestedOrientation
        act?.requestedOrientation = android.content.pm.ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
        onDispose { if (before != null) act.requestedOrientation = before }
    }
    HuntCamera(onExit, onBestiary)
}

@Composable
private fun HuntCamera(onExit: () -> Unit, onBestiary: () -> Unit) {
    val ctx = LocalContext.current
    val lifecycle = LocalLifecycleOwner.current
    val collected by HuntStore.collected.collectAsState()
    val shards by HuntStore.shards.collectAsState()
    val xp by HuntStore.xp.collectAsState()
    val muted by HuntStore.muted.collectAsState()
    val cmd by HuntStore.command.collectAsState()

    // ---- sensors: camera direction (yaw/pitch), light, shake + steps (accelerometer), sound level (mic, only if allowed) ----
    var yaw by remember { mutableFloatStateOf(0f) }
    var pitch by remember { mutableFloatStateOf(0f) }
    var lux by remember { mutableFloatStateOf(Float.NaN) }
    var shakeHits by remember { mutableIntStateOf(0) }       // strong jolts in the last ~1.5 s
    var steps by remember { mutableIntStateOf(0) }
    var loud by remember { mutableFloatStateOf(0f) }         // 0..1 microphone level
    val shakeTimes = remember { ArrayDeque<Long>() }
    DisposableEffect(Unit) {
        val sm = ctx.getSystemService(Context.SENSOR_SERVICE) as SensorManager
        val rv = sm.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR) ?: sm.getDefaultSensor(Sensor.TYPE_GAME_ROTATION_VECTOR)
        val acc = sm.getDefaultSensor(Sensor.TYPE_LINEAR_ACCELERATION) ?: sm.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
        val light = sm.getDefaultSensor(Sensor.TYPE_LIGHT)
        val r = FloatArray(9); val out = FloatArray(9); val o = FloatArray(3)
        var first = true; var lastStep = 0L; var gravity = 9.81f
        val l = object : SensorEventListener {
            override fun onSensorChanged(e: SensorEvent) {
                when (e.sensor.type) {
                    Sensor.TYPE_ROTATION_VECTOR, Sensor.TYPE_GAME_ROTATION_VECTOR -> {
                        SensorManager.getRotationMatrixFromVector(r, e.values)
                        SensorManager.remapCoordinateSystem(r, SensorManager.AXIS_X, SensorManager.AXIS_Z, out)   // camera looks along -Z
                        SensorManager.getOrientation(out, o)
                        val y = Math.toDegrees(o[0].toDouble()).toFloat(); val p = -Math.toDegrees(o[1].toDouble()).toFloat()
                        if (first) { yaw = y; pitch = p; first = false } else { yaw = wrap(yaw + wrap(y - yaw) * 0.25f); pitch += (p - pitch) * 0.25f }
                    }
                    Sensor.TYPE_LIGHT -> lux = e.values[0]
                    else -> {
                        val m = kotlin.math.sqrt(e.values[0] * e.values[0] + e.values[1] * e.values[1] + e.values[2] * e.values[2])
                        val a = if (e.sensor.type == Sensor.TYPE_ACCELEROMETER) { gravity = gravity * 0.9f + m * 0.1f; abs(m - gravity) } else m
                        val now = System.currentTimeMillis()
                        if (a > 2.2f && now - lastStep > 330) { lastStep = now; steps++ }            // a walking step
                        if (a > 11f) { shakeTimes.addLast(now) }                                     // a hard shake
                        while (shakeTimes.isNotEmpty() && now - shakeTimes.first() > 1500) shakeTimes.removeFirst()
                        shakeHits = shakeTimes.size
                    }
                }
            }
            override fun onAccuracyChanged(s: Sensor?, a: Int) {}
        }
        rv?.let { sm.registerListener(l, it, SensorManager.SENSOR_DELAY_GAME) }
        acc?.let { sm.registerListener(l, it, SensorManager.SENSOR_DELAY_GAME) }
        light?.let { sm.registerListener(l, it, SensorManager.SENSOR_DELAY_NORMAL) }
        onDispose { sm.unregisterListener(l) }
    }
    val micAllowed = remember { ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED }

    // ---- the one creature currently loose ----
    var wild by remember { mutableStateOf<Wild?>(null) }
    var t by remember { mutableFloatStateOf(0f) }
    var revealUntil by remember { mutableFloatStateOf(0f) }   // radar shows it after a SCAN
    var aiLine by remember { mutableStateOf<String?>(null) }
    var aiAt by remember { mutableFloatStateOf(-99f) }
    fun ai(text: String) { aiLine = text; aiAt = t }
    fun spawn(delaySec: Float = 0f) {
        val sp = HuntStore.nextSpecies(wild?.sp?.id) ?: run { wild = null; return }
        wild = Wild(sp, wrap(yaw + (if (Random.nextBoolean()) 1 else -1) * (60f + Random.nextFloat() * 110f)),
            Random.nextFloat() * 20f - 10f, 12f + Random.nextFloat() * 10f + sp.rarity.ordinal * 2f, Random.nextFloat() * 100f).also {
            it.moodUntil = t + delaySec
        }
        HuntSound.play("spot", 0.7f)
        ai("New ${sp.rarity.label.lowercase()} signature detected. SCAN to pin it down.")
    }

    // ---- intro: leaving the mine through the tunnel ----
    val intro = remember { Animatable(0f) }
    LaunchedEffect(Unit) {
        com.operator.companion.service.LivePreviewService.stop(ctx)   // one camera at a time
        CompanionWebSocketClient.activeInstance?.purgeHuntLeftovers()   // never keep photos on the phone
        HuntStore.active.value = true
        HuntSound.play("rumble")
        intro.animateTo(1f, tween(2600, easing = FastOutSlowInEasing))
        HuntSound.music(HuntSound.Music.EXPLORE)
        spawn()
    }
    DisposableEffect(Unit) { onDispose { HuntSound.music(HuntSound.Music.NONE) } }

    // ---- capture animation (containment beam → DNA helix) ----
    var capturing by remember { mutableStateOf<Species?>(null) }
    var capAt by remember { mutableFloatStateOf(0f) }
    var capPos by remember { mutableStateOf(Offset.Zero) }
    var acquired by remember { mutableStateOf<Triple<Species, Int, Int>?>(null) }   // species, shards, xp
    var toast by remember { mutableStateOf<String?>(null) }
    var flash by remember { mutableLongStateOf(0L) }
    LaunchedEffect(acquired) { if (acquired != null) { delay(3600); acquired = null } }
    LaunchedEffect(toast) { if (toast != null) { delay(2400); toast = null } }

    var size by remember { mutableStateOf(Size(1f, 1f)) }
    val reticle = Offset(size.width / 2, size.height * 0.45f)
    fun screenPos(w: Wild): Offset {
        val dx = wrap(w.bearing - yaw); val dy = w.elev - pitch
        val lunge = if (w.mood == Mood.ATTACK) 1f else 0f
        return Offset(size.width / 2 + dx / H_FOV * size.width, size.height * 0.45f - dy / V_FOV * size.height + lunge * 30f * sin(t * 30))
    }
    fun onScreen(w: Wild) = abs(wrap(w.bearing - yaw)) < H_FOV / 2 && abs(w.elev - pitch) < V_FOV / 2
    fun drawSize(w: Wild) = (size.width * 0.55f / w.dist.coerceAtLeast(2f)) * (if (w.mood == Mood.ATTACK) 1.25f else 1f)

    // ---- the game loop: creature behaviour + challenges ----
    var steadyFor by remember { mutableFloatStateOf(0f) }
    var darkFor by remember { mutableFloatStateOf(0f) }
    var lastYaw by remember { mutableFloatStateOf(0f) }
    var lastPitch by remember { mutableFloatStateOf(0f) }
    var lastSteps by remember { mutableIntStateOf(0) }
    var lockFor by remember { mutableFloatStateOf(0f) }
    LaunchedEffect(Unit) {
        val t0 = withFrameNanos { it }
        while (true) withFrameNanos { now ->
            val nt = (now - t0) / 1e9f
            val dt = (nt - t).coerceIn(0f, 0.1f)
            t = nt
            val w = wild ?: return@withFrameNanos
            if (capturing != null || intro.value < 1f) return@withFrameNanos
            val sp = w.sp; val spd = sp.rarity.speed
            val dx = wrap(w.bearing - yaw)
            val vis = onScreen(w)
            // steps: walking toward it closes the distance, walking away opens it
            val newSteps = steps - lastSteps; lastSteps = steps
            if (newSteps > 0) {
                if (abs(dx) < 35f) w.dist -= 0.85f * newSteps else if (abs(dx) > 145f) w.dist += 0.6f * newSteps
                w.dist = w.dist.coerceIn(1.5f, 40f)
            }
            if (vis) { w.seenFor += dt; w.offFor = 0f } else { w.seenFor = 0f; w.offFor += dt }
            // behaviour
            when (w.mood) {
                Mood.ROAM -> {
                    w.bearing = wrap(w.bearing + sin(t * 0.6f + w.seed) * 5f * spd * dt)
                    w.elev = (w.elev + cos(t * 0.8f + w.seed) * 3f * dt).coerceIn(-20f, 20f)
                    // it notices you (sooner when you're close) and bolts
                    if (vis && w.seenFor > (1.6f - spd * 0.4f + w.dist / 30f)) {
                        w.noticed = true
                        if (sp.challenge == Challenge.RECOVER || Random.nextInt(3) == 0) { w.mood = Mood.ATTACK; w.moodUntil = t + 0.7f }
                        else { w.mood = Mood.FLEE; w.moodUntil = t + 1.8f + Random.nextFloat() * 1.5f * spd; w.fleeDir = if (dx >= 0) 1f else -1f; HuntSound.play("flee", 0.6f) }
                    }
                }
                Mood.FLEE -> {
                    w.bearing = wrap(w.bearing + w.fleeDir * (18f + 16f * spd) * dt)
                    w.dist = (w.dist + 1.2f * spd * dt).coerceAtMost(40f)
                    w.elev = (w.elev + sin(t * 4 + w.seed) * 10f * dt).coerceIn(-20f, 20f)
                    if (t > w.moodUntil) { w.mood = Mood.REST; w.moodUntil = t + 3.5f + Random.nextFloat() * 2f; w.facingAway = Random.nextBoolean() }
                }
                Mood.ATTACK -> if (t > w.moodUntil) {
                    w.mood = Mood.REST; w.moodUntil = t + 3f + Random.nextFloat() * 1.5f
                    if (sp.challenge == Challenge.RECOVER) { w.openUntil = t + 3f; HuntSound.play("challenge", 0.6f); ai("It's recovering from the attack — STRIKE NOW!") }
                }
                Mood.REST -> {
                    if (sp.challenge == Challenge.FLANK && (t * 0.4f + w.seed).toInt() % 2 == 0 != w.facingAway) {
                        w.facingAway = !w.facingAway
                        if (w.facingAway) { w.openUntil = t + 2.5f; ai("It turned away — slow turner. STRIKE!") }
                    }
                    if (t > w.moodUntil) { w.mood = if (vis) Mood.FLEE else Mood.ROAM; w.moodUntil = t + 2.5f; w.fleeDir = if (Random.nextBoolean()) 1f else -1f; if (w.mood == Mood.FLEE) HuntSound.play("flee", 0.5f) }
                }
                Mood.LOST -> {}
            }
            // lost the trail: off screen too long → it relocates; SCAN to find it again
            if (w.offFor > 20f && t > revealUntil) {
                w.bearing = wrap(yaw + (if (Random.nextBoolean()) 1 else -1) * (90f + Random.nextFloat() * 80f))
                w.dist = (w.dist + 6f).coerceAtMost(28f); w.offFor = 0f; w.mood = Mood.ROAM; w.seenFor = 0f
                HuntSound.play("lost"); ai("Signal lost… it slipped away. SCAN the area!")
            }
            // weakness challenges (only count while it's in range and on screen)
            val inRange = vis && w.dist <= RANGE
            val moved = abs(wrap(yaw - lastYaw)) + abs(pitch - lastPitch); lastYaw = yaw; lastPitch = pitch
            steadyFor = if (moved < 0.35f) steadyFor + dt else 0f
            darkFor = if (!lux.isNaN() && lux < 8f) darkFor + dt else 0f
            if (inRange && t > w.openUntil) {
                val ch = sp.challenge
                val ok = when (ch) {
                    Challenge.STEADY -> steadyFor > 1.4f
                    Challenge.DARK -> if (lux.isNaN()) steadyFor > 1.4f else darkFor > 1.0f
                    Challenge.LOW -> pitch < -22f
                    Challenge.SHAKE -> shakeHits >= 3
                    Challenge.NOISE -> if (micAllowed) loud > 0.55f else shakeHits >= 3
                    Challenge.STEPS -> w.dist <= 3.2f
                    Challenge.ALL -> HuntStore.ORIGINAL_NINE.all { it in HuntStore.collected.value }
                    Challenge.CORE -> w.coreTaps >= 3
                    else -> false        // PULSE (button), RECOVER / FLANK (timing, handled above)
                }
                if (ok) { w.openUntil = t + 5f; HuntSound.play("challenge"); ai("WEAKNESS EXPOSED — ${sp.weakness.lowercase()}. Capture within 5 s!") }
            }
            // lock-on: in the crosshair, in range
            val p = screenPos(w)
            val inCross = vis && hypot(p.x - reticle.x, p.y - reticle.y) < size.width * 0.2f + drawSize(w) * 0.5f
            val was = lockFor >= 0.7f
            lockFor = if (inCross && w.dist <= RANGE) lockFor + dt else 0f
            if (!was && lockFor >= 0.7f) HuntSound.play("lock")
            HuntSound.music(when { lockFor >= 0.7f -> HuntSound.Music.LOCKED; vis -> HuntSound.Music.SPOTTED; else -> HuntSound.Music.EXPLORE })
        }
    }

    // ---- microphone level, only while a SONIC creature is in play ----
    val needMic = wild?.sp?.challenge == Challenge.NOISE && micAllowed
    LaunchedEffect(needMic) {
        if (!needMic) return@LaunchedEffect
        kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) {
            val sr = 16000
            val min = android.media.AudioRecord.getMinBufferSize(sr, android.media.AudioFormat.CHANNEL_IN_MONO, android.media.AudioFormat.ENCODING_PCM_16BIT)
            @Suppress("MissingPermission")
            val rec = runCatching { android.media.AudioRecord(android.media.MediaRecorder.AudioSource.MIC, sr, android.media.AudioFormat.CHANNEL_IN_MONO,
                android.media.AudioFormat.ENCODING_PCM_16BIT, maxOf(min, 4096)) }.getOrNull() ?: return@withContext
            if (rec.state != android.media.AudioRecord.STATE_INITIALIZED) { rec.release(); return@withContext }
            val buf = ShortArray(1024)
            try {
                rec.startRecording()
                while (kotlinx.coroutines.currentCoroutineContext()[kotlinx.coroutines.Job]?.isActive == true) {
                    val n = rec.read(buf, 0, buf.size)
                    var peak = 0; for (i in 0 until maxOf(0, n)) peak = maxOf(peak, abs(buf[i].toInt()))
                    loud = loud * 0.6f + (peak / 32768f) * 0.4f
                }
            } finally { runCatching { rec.stop() }; rec.release() }
        }
    }

    // ---- Tracker AI: drives the player with urgent directions ----
    LaunchedEffect(Unit) {
        while (true) {
            delay(4500)
            val w = wild ?: continue
            if (capturing != null || t - aiAt < 4f) continue
            val dx = wrap(w.bearing - yaw); val d = w.dist.toInt()
            val revealed = t < revealUntil
            ai(when {
                lockFor >= 0.7f && t < w.openUntil -> "LOCKED and EXPOSED — CAPTURE NOW!"
                lockFor >= 0.7f -> "Locked on ${w.sp.name}. ${w.sp.challenge.how}!"
                onScreen(w) && w.dist > RANGE -> "${w.sp.name} ${d} m ahead — MOVE FORWARD! Close to ${RANGE.toInt()} m."
                onScreen(w) && w.mood == Mood.FLEE -> "It's running ${if (w.fleeDir > 0) "RIGHT" else "LEFT"} — CHASE IT!"
                onScreen(w) -> "Target in sight. Centre it in the crosshair."
                revealed -> "Signal ${abs(dx).toInt()}° to your ${if (dx < 0) "LEFT" else "RIGHT"} — TURN ${if (dx < 0) "LEFT" else "RIGHT"}, ~$d m!"
                else -> listOf("Signal is weak. SCAN the area.", "Something moved nearby… SCAN!", "Keep moving — sweep the area slowly.").random()
            })
        }
    }

    // ---- camera: preview + frames to the laptop + shutter ----
    val preview = remember { PreviewView(ctx).apply { scaleType = PreviewView.ScaleType.FILL_CENTER } }
    val capture = remember { ImageCapture.Builder().setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY).setJpegQuality(95).build() }
    val exec = remember { Executors.newSingleThreadExecutor() }
    DisposableEffect(Unit) {
        val fut = ProcessCameraProvider.getInstance(ctx)
        var provider: ProcessCameraProvider? = null
        var lastSent = 0L
        val analysis = ImageAnalysis.Builder().setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888).build()
        analysis.setAnalyzer(exec) { img ->
            val client = CompanionWebSocketClient.activeInstance
            val now = System.currentTimeMillis()
            if (client != null && now - lastSent >= 140) {     // ~7 fps to the laptop, like Live Preview
                lastSent = now
                runCatching { val bm = rotated(img, 480); client.sendCameraFrame(jpeg64(bm, 55), "back", bm.width, bm.height) }
            }
            img.close()
        }
        fut.addListener({
            runCatching {
                val p = fut.get(); provider = p
                p.unbindAll()
                val pv = Preview.Builder().build().also { it.setSurfaceProvider(preview.surfaceProvider) }
                p.bindToLifecycle(lifecycle, CameraSelector.DEFAULT_BACK_CAMERA, pv, capture, analysis)
            }
        }, ContextCompat.getMainExecutor(ctx))
        onDispose {
            runCatching { provider?.unbindAll() }
            exec.shutdown()
            HuntStore.active.value = false
            CompanionWebSocketClient.activeInstance?.sendHuntState(JSONObject().put("active", false))
            CompanionWebSocketClient.activeInstance?.notifyCameraState(false, null)
        }
    }

    fun photo(speciesId: String?) {
        capture.takePicture(exec, object : ImageCapture.OnImageCapturedCallback() {
            override fun onCaptureSuccess(image: ImageProxy) {
                // full resolution: the camera's own JPEG, untouched (its EXIF keeps the right orientation)
                runCatching {
                    val bytes = if (image.format == android.graphics.ImageFormat.JPEG) {
                        val buf = image.planes[0].buffer; ByteArray(buf.remaining()).also { buf.get(it) }
                    } else ByteArrayOutputStream().also { rotated(image, 8192).compress(Bitmap.CompressFormat.JPEG, 95, it) }.toByteArray()
                    CompanionWebSocketClient.activeInstance?.sendHuntPhoto(bytes, speciesId)
                }
                image.close()
            }
            override fun onError(e: ImageCaptureException) {}
        })
    }

    val w0 = wild
    val locked = lockFor >= 0.7f
    val exposed = w0 != null && t < w0.openUntil

    fun shoot() {
        if (capturing != null) return
        flash = System.currentTimeMillis()
        val w = wild
        when {
            w == null -> { toast = "No DNA signature in range — photo saved"; photo(null) }
            !locked && onScreen(w) && w.dist > RANGE -> { toast = "Too far (${w.dist.toInt()} m) — move closer!"; HuntSound.play("fail"); photo(null) }
            !locked -> { toast = "No lock — centre it in the crosshair"; HuntSound.play("fail"); photo(null) }
            !exposed -> {
                // a failed grab spooks it
                toast = "It shrugged it off! ${w.sp.challenge.how}"; HuntSound.play("fail")
                w.mood = Mood.FLEE; w.moodUntil = t + 2f; w.fleeDir = if (Random.nextBoolean()) 1f else -1f; HuntSound.play("flee", 0.6f)
                photo(null)
            }
            else -> {
                capturing = w.sp; capAt = t; capPos = screenPos(w)
                HuntSound.music(HuntSound.Music.NONE); HuntSound.play("beam")
                photo(w.sp.id)
            }
        }
    }
    // finish the capture animation → collect + rewards → next creature
    LaunchedEffect(capturing) {
        val sp = capturing ?: return@LaunchedEffect
        delay(1700)
        val (s, x) = HuntStore.collect(ctx, sp.id)
        HuntSound.play("capture")
        acquired = Triple(sp, s, x)
        capturing = null; wild = null; lockFor = 0f
        delay(2500)
        HuntSound.music(HuntSound.Music.EXPLORE)
        spawn()
    }

    fun scan() {
        HuntSound.play("scan")
        revealUntil = t + 8f
        val w = wild
        if (Random.nextInt(4) == 0) { val n = 1 + Random.nextInt(3); HuntStore.addShards(ctx, n); HuntSound.play("shard"); toast = "💎 +$n crystal shards found while scanning" }
        if (w != null) { val dx = wrap(w.bearing - yaw); ai("PING: ${w.sp.rarity.label} signature ${abs(dx).toInt()}° ${if (dx < 0) "LEFT" else "RIGHT"}, ${w.dist.toInt()} m. GO!") }
        else ai("No signatures nearby.")
    }

    fun pulse() {
        val w = wild ?: return
        if (w.sp.challenge != Challenge.PULSE || !onScreen(w) || w.dist > RANGE) { toast = "Pulse only works on a pulse-weak target in range"; HuntSound.play("fail"); return }
        if (!HuntStore.spend(ctx, 5)) { toast = "Not enough crystal shards (need 5 💎) — SCAN to find more"; HuntSound.play("fail"); return }
        HuntSound.play("pulse"); w.openUntil = t + 5f; HuntSound.play("challenge"); ai("Pulse hit! Weakness exposed — CAPTURE!")
    }

    // ---- live state to the laptop (1/s) ----
    LaunchedEffect(Unit) {
        while (true) {
            delay(1000)
            val client = CompanionWebSocketClient.activeInstance ?: continue
            val w = wild
            val list = JSONArray()
            if (w != null) {
                val dx = wrap(w.bearing - yaw)
                list.put(JSONObject().put("id", w.sp.id).put("name", w.sp.name).put("rarity", w.sp.rarity.label).put("dx", dx.toInt())
                    .put("dy", (w.elev - pitch).toInt()).put("on_screen", onScreen(w)).put("dist", w.dist.toInt()).put("mood", w.mood.name.lowercase())
                    .put("challenge", w.sp.challenge.label).put("how", w.sp.challenge.how).put("exposed", t < w.openUntil))
            }
            client.sendHuntState(JSONObject().put("active", true).put("dna", HuntStore.collected.value.size).put("total", SPECIES.size)
                .put("shards", HuntStore.shards.value).put("xp", HuntStore.xp.value).put("level", HuntStore.level())
                .put("target", if (w != null && locked) w.sp.name else JSONObject.NULL).put("locked", locked).put("creatures", list)
                .put("ai", aiLine ?: JSONObject.NULL))
        }
    }

    Box(Modifier.fillMaxSize().background(Color.Black)) {
        AndroidView({ preview }, Modifier.fillMaxSize())

        Canvas(Modifier.fillMaxSize().pointerInput(Unit) {
            detectTapGestures { tap ->   // CORE challenge: tap the creature's crystal core
                val w = wild ?: return@detectTapGestures
                if (w.sp.challenge == Challenge.CORE && onScreen(w) && w.dist <= RANGE) {
                    val p = screenPos(w)
                    if (hypot(tap.x - p.x, tap.y - p.y) < drawSize(w) * 1.2f) { w.coreTaps++; HuntSound.play("tick", 2f); if (w.coreTaps < 3) toast = "Core hit ${w.coreTaps}/3" }
                }
            }
        }) {
            size = this.size
            drawRect(Brush.verticalGradient(listOf(Color(0x5507081A), Color.Transparent, Color(0x6607081A))))
            val w = wild
            if (w != null && capturing == null && onScreen(w)) {
                val p = screenPos(w); val s = drawSize(w)
                if (t < w.openUntil) drawCircle(Gold.copy(alpha = 0.25f + 0.15f * sin(t * 12)), s * 2.2f, p)   // weakness exposed glow
                if (w.mood == Mood.ATTACK) drawCircle(Color(0x55EF4444), s * 2.6f, p)
                drawSpecies(w.sp, p, s, t + w.seed, facingLeft = if (w.mood == Mood.FLEE) w.fleeDir < 0 else w.facingAway)
                if (w.sp.challenge == Challenge.CORE && w.dist <= RANGE) drawCircle(Color(0xFFF0ABFC).copy(alpha = 0.5f + 0.4f * sin(t * 8)), s * 0.22f, p, style = Stroke(5f))
            }
            // capture: beam from the shutter + the creature spiralling into a DNA helix
            capturing?.let { sp ->
                val k = ((t - capAt) / 1.6f).coerceIn(0f, 1f)
                val c = capPos + (reticle - capPos) * k
                val base = Offset(size.width / 2, size.height - 120f)
                for (i in 0 until 3) drawLine(Cyan.copy(alpha = 0.5f - i * 0.12f), base, c, 14f - i * 4f, StrokeCap.Round)
                drawSpecies(sp, c, size.width * 0.09f * (1f - k * 0.85f), t * 3)
                for (i in 0 until 26) {   // double helix
                    val y = c.y - 140f + i * 11f; val ph = t * 9 + i * 0.5f
                    val r = 60f * (1f - k * 0.5f)
                    drawCircle(Cyan, 5f, Offset(c.x + sin(ph) * r, y)); drawCircle(Violet, 5f, Offset(c.x - sin(ph) * r, y))
                    if (i % 3 == 0) drawLine(Color.White.copy(alpha = 0.4f), Offset(c.x + sin(ph) * r, y), Offset(c.x - sin(ph) * r, y), 2f)
                }
                drawCircle(Gold.copy(alpha = k), 40f + k * size.width * 0.4f, c, style = Stroke(6f * (1f - k) + 1f))
            }
            // reticle (gold when locked, pulsing when the weakness is exposed)
            val r = size.width * 0.17f
            val col = when { locked && exposed -> Gold; locked -> Color(0xFFFB923C); else -> Cyan }
            val kk = r * 0.35f
            for ((sx, sy) in listOf(-1f to -1f, 1f to -1f, -1f to 1f, 1f to 1f)) {
                val c = reticle + Offset(sx * r, sy * r)
                drawLine(col, c, c + Offset(-sx * kk, 0f), 6f, StrokeCap.Round); drawLine(col, c, c + Offset(0f, -sy * kk), 6f, StrokeCap.Round)
            }
            drawLine(col.copy(alpha = 0.8f), reticle + Offset(-18f, 0f), reticle + Offset(18f, 0f), 3f)
            drawLine(col.copy(alpha = 0.8f), reticle + Offset(0f, -18f), reticle + Offset(0f, 18f), 3f)
            val lp = (lockFor / 0.7f).coerceIn(0f, 1f)
            if (lp > 0f) drawArc(col, -90f, 360f * lp, false, reticle - Offset(r * 0.6f, r * 0.6f), Size(r * 1.2f, r * 1.2f), style = Stroke(7f, cap = StrokeCap.Round))
            // edge arrow toward a revealed signature
            if (w != null && !onScreen(w) && t < revealUntil) {
                val dx = wrap(w.bearing - yaw)
                val x = if (dx < 0) 40f else size.width - 40f
                val y = reticle.y
                val d = if (dx < 0) -1f else 1f
                drawLine(w.sp.rarity.color, Offset(x - d * 30f, y - 30f), Offset(x, y), 8f, StrokeCap.Round)
                drawLine(w.sp.rarity.color, Offset(x - d * 30f, y + 30f), Offset(x, y), 8f, StrokeCap.Round)
            }
            val scanY = (t * 0.35f % 1f) * size.height
            drawLine(Cyan.copy(alpha = 0.16f), Offset(0f, scanY), Offset(size.width, scanY), 3f)
        }

        // ---------- HUD ----------
        Column(Modifier.fillMaxWidth().statusBarsPadding().padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                HudChip("←", onClick = onExit)
                Spacer(Modifier.width(6.dp))
                HudChip("🧬 ${collected.size}/${SPECIES.size}")
                Spacer(Modifier.width(6.dp))
                HudChip("💎 $shards")
                Spacer(Modifier.width(6.dp))
                HudChip(if (muted) "🔇" else "🔊", onClick = { HuntStore.toggleMute(ctx); if (!HuntStore.muted.value) HuntSound.music(HuntSound.Music.EXPLORE) })
                Spacer(Modifier.weight(1f))
                val w = wild
                Radar(if (w != null && (t < revealUntil || onScreen(w))) listOf(wrap(w.bearing - yaw) to w.sp.rarity.color) else emptyList(), Modifier.size(80.dp))
            }
            // level / XP
            Row(Modifier.padding(top = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                Text("LV ${HuntStore.level(xp)}", color = Gold, fontWeight = FontWeight.Black, fontSize = 12.sp)
                Spacer(Modifier.width(6.dp))
                Box(Modifier.width(140.dp).height(6.dp).clip(RoundedCornerShape(3.dp)).background(Color(0x55FFFFFF))) {
                    Box(Modifier.fillMaxWidth(HuntStore.levelProgress(xp)).fillMaxHeight().background(Brush.horizontalGradient(listOf(Gold, Color(0xFFF97316)))))
                }
            }
            // Tracker AI (the game's own voice) — laptop messages show as HQ overrides
            val laptop = cmd?.takeIf { System.currentTimeMillis() - it.id < 8000 }
            if (laptop != null) TrackerBanner("🛰 TRACKER AI · HQ OVERRIDE", laptop.text.ifBlank { arrowText(laptop.arrow) }, laptop.arrow, t)
            else aiLine?.takeIf { t - aiAt < 4.2f }?.let { TrackerBanner("🛰 TRACKER AI", it, null, t) }
        }

        // target card
        wild?.takeIf { onScreen(it) && capturing == null }?.let { w ->
            val inR = w.dist <= RANGE
            Column(Modifier.align(Alignment.BottomCenter).padding(bottom = 170.dp).padding(horizontal = 16.dp).fillMaxWidth()
                .clip(RoundedCornerShape(14.dp)).background(Panel).border(1.dp, w.sp.rarity.color.copy(alpha = 0.7f), RoundedCornerShape(14.dp)).padding(12.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(if (w.sp.id in collected) w.sp.name else "??? · ${w.sp.rarity.label}", color = Color.White, fontWeight = FontWeight.Black, fontSize = 15.sp, modifier = Modifier.weight(1f))
                    Text("${w.dist.toInt()} m", color = if (inR) Color(0xFF4ADE80) else Color(0xFFFB923C), fontSize = 14.sp, fontWeight = FontWeight.Black)
                }
                Text("⚡ ${w.sp.power} · ${w.mood.name.lowercase()}", color = Color(0xFFCBD5E1), fontSize = 12.sp)
                Text("🎯 ${w.sp.challenge.label}: ${w.sp.challenge.how}", color = if (exposed) Gold else Color(0xFFE2E8F0), fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                Text(when { !inR -> "Get within ${RANGE.toInt()} m — walk toward it"; exposed && locked -> "EXPOSED + LOCKED — CAPTURE DNA!"; exposed -> "EXPOSED — lock on fast!"; locked -> "Locked — now exploit the weakness"; else -> "Centre it to lock on" },
                    color = if (exposed) Gold else Cyan, fontSize = 11.sp, fontWeight = FontWeight.Black)
            }
        }

        // bottom controls
        Row(Modifier.align(Alignment.BottomCenter).fillMaxWidth().navigationBarsPadding().padding(horizontal = 18.dp, vertical = 18.dp),
            verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
            RoundHud("📖", "BESTIARY", onBestiary)
            if (wild?.sp?.challenge == Challenge.PULSE) RoundHud("⚡", "PULSE 5💎") { pulse() }
            Shutter(locked && exposed) { shoot() }
            RoundHud("📡", "SCAN") { scan() }
        }

        toast?.let {
            Text(it, color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, textAlign = TextAlign.Center,
                modifier = Modifier.align(Alignment.Center).padding(top = 250.dp).padding(horizontal = 20.dp).clip(RoundedCornerShape(12.dp)).background(Panel).padding(horizontal = 14.dp, vertical = 8.dp))
        }

        acquired?.let { (sp, s, x) ->
            Column(Modifier.align(Alignment.Center).padding(24.dp).clip(RoundedCornerShape(18.dp))
                .background(Brush.verticalGradient(listOf(Color(0xEE1E1B4B), Color(0xEE0B1024)))).border(2.dp, sp.rarity.color, RoundedCornerShape(18.dp)).padding(18.dp),
                horizontalAlignment = Alignment.CenterHorizontally) {
                Text("🧬 DNA SEQUENCED", color = Gold, fontWeight = FontWeight.Black, fontSize = 18.sp, letterSpacing = 2.sp)
                Canvas(Modifier.size(120.dp)) { drawSpecies(sp, center, size.minDimension * 0.26f, t) }
                Text(sp.name, color = Color.White, fontWeight = FontWeight.Black, fontSize = 20.sp)
                Text("${sp.dna} · ${sp.rarity.label}", color = sp.rarity.color, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                Text("+$s 💎   +$x XP", color = Gold, fontSize = 15.sp, fontWeight = FontWeight.Black)
                Text("${collected.size} / ${SPECIES.size} collected · photo sent to the laptop", color = Color(0xFF94A3B8), fontSize = 11.sp)
            }
        }

        val fa = ((260 - (System.currentTimeMillis() - flash)) / 260f).coerceIn(0f, 1f)
        if (fa > 0f && t >= 0f) Box(Modifier.fillMaxSize().alpha(fa * 0.8f).background(Color.White))

        // intro: dark tunnel, the light at its end grows until you're out of the mine
        if (intro.value < 1f) Canvas(Modifier.fillMaxSize()) {
            val p = intro.value
            val hole = size.minDimension * (0.06f + p * p * 1.4f)
            val c = Offset(size.width / 2, size.height * 0.45f)
            drawRect(Brush.radialGradient(listOf(Color.Transparent, Color(0xFF05060C)), c, hole))
            for (i in 0 until 4) {
                val z = ((p * 2.2f + i * 0.25f) % 1f)
                val ww = size.width * (0.2f + z * 1.4f); val hh = size.height * (0.16f + z * 1.1f)
                drawRect(Color(0xFF8B5A2B).copy(alpha = (1f - p) * 0.8f), Offset(c.x - ww / 2, c.y - hh / 2), Size(ww, hh), style = Stroke(10f + z * 22f))
            }
        }
        if (intro.value < 1f) Text(if (intro.value < 0.6f) "⛏ LEAVING THE MINES…" else "☀ SURFACE REACHED — signatures detected",
            color = Color.White, fontWeight = FontWeight.Black, fontSize = 15.sp, letterSpacing = 1.sp, textAlign = TextAlign.Center,
            modifier = Modifier.align(Alignment.BottomCenter).fillMaxWidth().padding(start = 16.dp, end = 16.dp, bottom = 120.dp))
    }
}

// ---------- pieces ----------

private fun arrowText(a: String?) = mapOf("left" to "TURN LEFT — NOW!", "right" to "TURN RIGHT — NOW!", "up" to "LOOK UP!", "down" to "LOOK DOWN!",
    "forward" to "MOVE FORWARD — FAST!", "back" to "STEP BACK!", "scan" to "SCAN THIS AREA!")[a] ?: ""

/** The Tracker AI banner: urgent, pulsing, with a direction arrow when given. */
@Composable
private fun TrackerBanner(title: String, text: String, arrow: String?, t: Float) {
    val glyph = when (arrow) { "left" -> "⬅"; "right" -> "➡"; "up" -> "⬆"; "down" -> "⬇"; "forward" -> "⏫"; "back" -> "⏬"; "scan" -> "📡"; else -> "🛰" }
    val pulse = 0.65f + 0.35f * ((sin(t * 7) + 1f) / 2f)
    Row(Modifier.padding(top = 8.dp).fillMaxWidth().clip(RoundedCornerShape(14.dp))
        .background(Brush.horizontalGradient(listOf(Color(0xEE3B0764), Color(0xEE7F1D1D))))
        .border(2.dp, Color(0xFFF43F5E).copy(alpha = pulse), RoundedCornerShape(14.dp)).padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(glyph, fontSize = 28.sp, modifier = Modifier.alpha(pulse))
        Spacer(Modifier.width(10.dp))
        Column {
            Text(title, color = Color(0xFFFDA4AF), fontSize = 10.sp, fontWeight = FontWeight.Black, letterSpacing = 2.sp)
            Text(text, color = Color.White, fontSize = 15.sp, fontWeight = FontWeight.Black)
        }
    }
}

@Composable
private fun Radar(dots: List<Pair<Float, Color>>, modifier: Modifier) {
    val sweep by rememberInfiniteTransition(label = "radar").animateFloat(0f, 360f, infiniteRepeatable(tween(2400, easing = LinearEasing)), label = "s")
    Canvas(modifier.clip(CircleShape).background(Panel).border(1.dp, Cyan.copy(alpha = 0.6f), CircleShape)) {
        val c = center; val r = size.minDimension / 2
        drawCircle(Cyan.copy(alpha = 0.25f), r * 0.5f, c, style = Stroke(1.5f))
        drawArc(Brush.sweepGradient(listOf(Color.Transparent, Cyan.copy(alpha = 0.35f)), c), sweep - 50f, 50f, true)
        // field of view wedge (straight up = where the camera looks)
        drawArc(Color(0x3322D3EE), -90f - H_FOV / 2, H_FOV, true)
        dots.forEach { (dx, col) ->
            val a = Math.toRadians((dx - 90f).toDouble())
            drawCircle(col, 5f, c + Offset((cos(a) * r * 0.72f).toFloat(), (sin(a) * r * 0.72f).toFloat()))
        }
        drawCircle(Cyan, 4f, c)
    }
}

@Composable
private fun Shutter(locked: Boolean, onClick: () -> Unit) {
    val pulse by rememberInfiniteTransition(label = "sh").animateFloat(1f, 1.1f, infiniteRepeatable(tween(700), RepeatMode.Reverse), label = "p")
    val ring = if (locked) Gold else Cyan
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Box(Modifier.size((84 * (if (locked) pulse else 1f)).dp).clip(CircleShape)
            .background(Brush.radialGradient(listOf(ring.copy(alpha = 0.9f), Color(0xFF0B1024))))
            .border(4.dp, ring, CircleShape).clickable(onClick = onClick), contentAlignment = Alignment.Center) {
            Text("🧬", fontSize = 32.sp)
        }
        Text("CAPTURE DNA", color = ring, fontSize = 11.sp, fontWeight = FontWeight.Black, letterSpacing = 1.sp)
    }
}

@Composable
private fun RoundHud(icon: String, label: String, onClick: () -> Unit) {
    Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.clickable(onClick = onClick)) {
        Box(Modifier.size(54.dp).clip(CircleShape).background(Panel).border(1.dp, Cyan.copy(alpha = 0.6f), CircleShape), contentAlignment = Alignment.Center) {
            Text(icon, fontSize = 22.sp)
        }
        Text(label, color = Color(0xFFCBD5E1), fontSize = 10.sp, fontWeight = FontWeight.Bold)
    }
}

@Composable
private fun HudChip(text: String, onClick: (() -> Unit)? = null) {
    Box(Modifier.clip(RoundedCornerShape(12.dp)).background(Panel).border(1.dp, Cyan.copy(alpha = 0.5f), RoundedCornerShape(12.dp))
        .then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier).padding(horizontal = 12.dp, vertical = 8.dp)) {
        Text(text, color = Color.White, fontSize = 14.sp, fontWeight = FontWeight.Bold)
    }
}

@Composable
fun HudButton(text: String, color: Color, onClick: () -> Unit) {
    Box(Modifier.clip(RoundedCornerShape(26.dp)).background(Brush.horizontalGradient(listOf(color, Violet))).clickable(onClick = onClick)
        .padding(horizontal = 26.dp, vertical = 14.dp)) {
        Text(text, color = Color.White, fontWeight = FontWeight.Black, letterSpacing = 1.sp)
    }
}

// ---------- image helpers ----------
private fun rotated(img: ImageProxy, maxSide: Int): Bitmap {
    val src = img.toBitmap()
    val deg = img.imageInfo.rotationDegrees
    val scale = (maxSide.toFloat() / maxOf(src.width, src.height)).coerceAtMost(1f)
    val m = Matrix().apply { postScale(scale, scale); if (deg != 0) postRotate(deg.toFloat()) }
    return Bitmap.createBitmap(src, 0, 0, src.width, src.height, m, true)
}

private fun jpeg64(bm: Bitmap, q: Int): String {
    val out = ByteArrayOutputStream()
    bm.compress(Bitmap.CompressFormat.JPEG, q, out)
    return Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)
}
