package com.operator.companion.hunt

import android.graphics.Bitmap
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.label.ImageLabeling
import com.google.mlkit.vision.label.defaults.ImageLabelerOptions
import com.google.mlkit.vision.objects.ObjectDetection
import com.google.mlkit.vision.objects.defaults.ObjectDetectorOptions
import kotlinx.coroutines.flow.MutableStateFlow

/** Where a species likes to appear. */
enum class Habitat { SKY, GROUND, PLANT, WATER }

fun Species.habitat(): Habitat = when (body) {
    Body.BAT, Body.MOTH, Body.EAGLE, Body.PHOENIX, Body.FIREFLY, Body.HORNET, Body.MOSQUITO, Body.DRONE, Body.MANTIS, Body.WRAITH -> Habitat.SKY
    Body.PLANT, Body.MUSHROOM, Body.HIVE -> Habitat.PLANT
    Body.EEL, Body.SHARK, Body.OCTOPUS, Body.ANGLER, Body.MANTA, Body.ANEMONE, Body.STARFISH, Body.JELLY, Body.SQUID, Body.CRAB, Body.CROCODILE, Body.FROG -> Habitat.WATER
    else -> Habitat.GROUND
}

/**
 * On-device scene understanding for the HUNT (Google ML Kit, runs on the phone):
 *  - image labelling → what's around (door, stairs, couch / road, car, tree, sky …) → INDOOR or OUTDOOR
 *  - object detection → where real objects are; each becomes a world ANCHOR (compass bearing + elevation)
 *    so creatures can hide behind a door, under a car, in a plant, up near the ceiling or the sky.
 */
object SceneSense {
    enum class Kind { UNKNOWN, INDOOR, OUTDOOR }
    /** A real object pinned in the world. */
    data class Anchor(val name: String, val bearing: Float, val elev: Float, val top: Float, val bottom: Float, val plant: Boolean, val at: Long)

    val kind = MutableStateFlow(Kind.UNKNOWN)
    val labels = MutableStateFlow<List<String>>(emptyList())
    val anchors = MutableStateFlow<List<Anchor>>(emptyList())

    private val INDOOR = setOf("room", "furniture", "couch", "bed", "table", "desk", "shelf", "door", "window", "stairs", "curtain",
        "television", "computer", "monitor", "lamp", "cushion", "kitchen", "cupboard", "bathroom", "wall", "ceiling", "chair", "musical instrument",
        "tableware", "cup", "bookcase", "home good", "interior design", "loveseat", "pillow", "fridge", "toilet")
    private val OUTDOOR = setOf("sky", "cloud", "road", "car", "vehicle", "bicycle", "motorcycle", "tree", "grass", "plant", "flower", "building",
        "skyscraper", "street", "bridge", "mountain", "lake", "river", "beach", "sea", "garden", "forest", "field", "playground", "bus",
        "truck", "train", "sun", "pond", "leaf", "soil", "snow", "fence", "wheel", "tire", "lighthouse", "waterfall")
    /** Labels worth naming in the Tracker AI ("near the door"). */
    private val NAMEABLE = setOf("door", "stairs", "window", "couch", "bed", "table", "desk", "shelf", "curtain", "television", "lamp",
        "chair", "car", "vehicle", "bicycle", "motorcycle", "tree", "plant", "flower", "building", "road", "fence", "bus", "truck",
        "grass", "garden", "bench", "person", "wall", "cupboard", "fridge", "computer", "monitor")

    private val labeler by lazy { ImageLabeling.getClient(ImageLabelerOptions.Builder().setConfidenceThreshold(0.55f).build()) }
    private val detector by lazy {
        ObjectDetection.getClient(ObjectDetectorOptions.Builder().setDetectorMode(ObjectDetectorOptions.STREAM_MODE)
            .enableMultipleObjects().enableClassification().build())
    }
    @Volatile private var busy = false
    private var indoorScore = 0f   // smoothed: +1 indoor … -1 outdoor

    fun reset() { kind.value = Kind.UNKNOWN; labels.value = emptyList(); anchors.value = emptyList(); indoorScore = 0f }

    /** Analyse one upright camera frame taken while the camera pointed at (yaw, pitch). Non-blocking. */
    fun analyse(bm: Bitmap, yaw: Float, pitch: Float, hFov: Float, vFov: Float) {
        if (busy) return
        busy = true
        val img = InputImage.fromBitmap(bm, 0)
        labeler.process(img).addOnSuccessListener { list ->
            var score = 0f
            val names = list.sortedByDescending { it.confidence }.map { it.text.lowercase() }
            list.forEach { l -> val n = l.text.lowercase()
                if (n in INDOOR) score += l.confidence; if (n in OUTDOOR) score -= l.confidence }
            if (list.isNotEmpty()) indoorScore = indoorScore * 0.7f + score.coerceIn(-2f, 2f) * 0.3f
            kind.value = when { indoorScore > 0.18f -> Kind.INDOOR; indoorScore < -0.18f -> Kind.OUTDOOR; else -> kind.value }
            labels.value = names.take(6)
        }.addOnCompleteListener {
            detector.process(img).addOnSuccessListener { objs ->
                val now = System.currentTimeMillis()
                val named = labels.value.firstOrNull { it in NAMEABLE }
                val fresh = objs.map { o ->
                    val b = o.boundingBox
                    val cx = b.exactCenterX() / bm.width; val cy = b.exactCenterY() / bm.height
                    val cls = o.labels.maxByOrNull { it.confidence }?.text?.lowercase()
                    Anchor(name = (cls?.takeIf { it != "home good" && it != "place" } ?: named ?: cls ?: "object").replace("home good", "furniture"),
                        bearing = wrapDeg(yaw + (cx - 0.5f) * hFov), elev = pitch - (cy - 0.45f) * vFov,
                        top = pitch - (b.top.toFloat() / bm.height - 0.45f) * vFov, bottom = pitch - (b.bottom.toFloat() / bm.height - 0.45f) * vFov,
                        plant = cls == "plant", at = now)
                }
                // keep recent anchors from other directions too (the world doesn't vanish when you look away)
                anchors.value = (fresh + anchors.value.filter { old -> now - old.at < 90_000 && fresh.none { kotlin.math.abs(wrapDeg(it.bearing - old.bearing)) < 12f } }).take(24)
            }.addOnCompleteListener { busy = false }
        }
    }

    fun wrapDeg(d: Float): Float { var x = d % 360f; if (x > 180f) x -= 360f; if (x < -180f) x += 360f; return x }

    /** The best place for this species right now (bearing, elevation, a name to announce) — or null (no anchors yet). */
    fun placeFor(sp: Species, yaw: Float): Triple<Float, Float, String>? {
        val list = anchors.value
        val sky = kind.value == Kind.OUTDOOR
        return when (sp.habitat()) {
            Habitat.SKY -> {   // above an object (ceiling / sky), else straight up ahead
                val a = list.randomOrNull()
                if (a != null) Triple(a.bearing, (a.top + 12f).coerceAtMost(30f), if (sky) "in the sky above the ${a.name}" else "up above the ${a.name}")
                else Triple(wrapDeg(yaw + listOf(-70f, 70f, 120f, -120f).random()), 24f, if (sky) "up in the sky" else "near the ceiling")
            }
            Habitat.PLANT -> list.filter { it.plant || it.name.contains("plant") || it.name.contains("flower") || it.name.contains("tree") }.randomOrNull()
                ?.let { Triple(it.bearing, it.elev, "inside the ${it.name}") }
                ?: list.randomOrNull()?.let { Triple(it.bearing, it.bottom + 3f, "at the foot of the ${it.name}") }
            Habitat.WATER, Habitat.GROUND -> list.randomOrNull()?.let {
                val where = if (sky) listOf("under the ${it.name}", "behind the ${it.name}", "next to the ${it.name}").random()
                            else listOf("behind the ${it.name}", "under the ${it.name}", "beside the ${it.name}").random()
                Triple(it.bearing + listOf(-6f, 6f).random(), (it.bottom + 2f).coerceIn(-28f, 10f), where)
            }
        }
    }
}
