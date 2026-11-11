package com.operator.companion.hunt

import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.sin

/** Body plan used to draw a species — minimal glowing 2D shapes (no 3D models: light on battery and GPU). */
enum class Body { SPIDER, CENTIPEDE, MOTH, RAT, SCORPION, BAT, FROG, MOLE, OVERLORD,
    SERPENT, JELLY, MANTIS, GOLEM, WORM, PLANT, ORB, SQUID, WRAITH, HYDRA, BULL,
    // real-world inspired body plans — every species looks different
    WEBSPINNER, CRAB, ARMADILLO, HEDGEHOG, GORILLA, TURTLE, ELEPHANT, RHINO, EEL, LIZARD, SHARK, OCTOPUS, ANGLER,
    HORNET, CATERPILLAR, ANT, MUSHROOM, ANEMONE, HIVE, EGG, DRONE, STARFISH, FIREFLY, BEETLE, PHOENIX, DEER, EAGLE,
    MANTA, MOSQUITO, CROCODILE }

/** The aura drawn around a species, from its power. */
enum class Aura { FIRE, ICE, ELECTRIC, VOID, POISON, LIGHT, EARTH, SONIC, PSYCHIC, WATER, SHADOW, CRYSTAL }

enum class Rarity(val label: String, val color: Color, val speed: Float) {
    COMMON("COMMON", Color(0xFF4ADE80), 0.6f),
    UNCOMMON("UNCOMMON", Color(0xFFFBBF24), 0.9f),
    RARE("RARE", Color(0xFFF43F5E), 1.25f),
    EPIC("EPIC", Color(0xFFA855F7), 1.5f),
    LEGENDARY("LEGENDARY", Color(0xFFFACC15), 1.8f),
}

/** How a species' weakness is exploited before its DNA can be taken — each uses a real phone sensor/action. */
enum class Challenge(val label: String, val how: String) {
    STEADY("STEADY AIM", "Hold the phone perfectly still"),
    DARK("DARKNESS", "Cover the light — it's blind in the dark"),
    LOW("UNDERSIDE", "Aim low — strike its exposed underside"),
    SHAKE("VIBRATION", "Shake the phone to send a ground pulse"),
    NOISE("SONIC", "Make a loud noise to disrupt it"),
    STEPS("CLOSE IN", "Walk closer — it can't hit at close range"),
    LIGHT("BRIGHT LIGHT", "Shine a bright light at it (torch / sunlight)"),
    HIGH("HIGH GROUND", "Aim high — strike from above"),
    RANGED("KEEP DISTANCE", "Stay 4–6 m away and hold steady"),
    SCAN("AREA SCAN", "SCAN while it's in range to reveal it"),
    PULSE("ENERGY PULSE", "Fire an energy pulse (costs 5 💎)"),
    CORE("CRYSTAL CORE", "Tap its glowing crystal core 3 times"),
    RECOVER("RECOVERY", "Strike while it recovers after attacking"),
    FLANK("SLOW TURN", "Wait until it turns away, then strike"),
    ALL("DOMINION", "Needs the 9 original DNA signatures"),
}

data class Species(
    val id: String, val name: String, val dna: String, val rarity: Rarity, val body: Body,
    val power: String, val powerInfo: String, val weakness: String,
    val color: Color, val crystal: Color,
    val challenge: Challenge = Challenge.STEADY,
    val aura: Aura = Aura.CRYSTAL,
    val series: Int = 1,
)

val SPECIES = listOf(
    Species("crystalclaw", "CRYSTALCLAW", "CCM-001", Rarity.COMMON, Body.SPIDER, "Crystal Charge",
        "Rushes at its target with crystal armour.", "Exposed underside", Color(0xFF1E3A8A), Color(0xFF60A5FA), Challenge.LOW, Aura.CRYSTAL),
    Species("shardfang", "SHARDFANG", "SDF-002", Rarity.COMMON, Body.WEBSPINNER, "Web Trap",
        "Slows prey with a sticky web.", "Fire or an energy pulse", Color(0xFF4C1D95), Color(0xFFE879F9), Challenge.PULSE, Aura.VOID),
    Species("deepcrawler", "DEEPCRAWLER", "DPC-003", Rarity.UNCOMMON, Body.CENTIPEDE, "Burrow Dash",
        "Digs and strikes from underground.", "Ground vibration scanner", Color(0xFF1F2937), Color(0xFF38BDF8), Challenge.SHAKE, Aura.EARTH),
    Species("glowwing", "GLOWWING", "GLW-004", Rarity.UNCOMMON, Body.MOTH, "Blinding Flare",
        "Disorients with a bright flash.", "Darkness", Color(0xFF6D28D9), Color(0xFF93C5FD), Challenge.DARK, Aura.LIGHT),
    Species("rockmaw", "ROCKMAW", "RKM-005", Rarity.UNCOMMON, Body.RAT, "Tunnelling Rush",
        "Burrows and strikes suddenly.", "Slow turning", Color(0xFF374151), Color(0xFF60A5FA), Challenge.FLANK, Aura.EARTH),
    Species("venomshard", "VENOMSHARD", "VNS-006", Rarity.RARE, Body.SCORPION, "Venom Sting",
        "Poisons its target.", "Long recovery after an attack", Color(0xFF581C87), Color(0xFFD946EF), Challenge.RECOVER, Aura.POISON),
    Species("nightglider", "NIGHTGLIDER", "NTG-007", Rarity.RARE, Body.BAT, "Echo Dive",
        "Falls from above with a sonic strike.", "Sonic disruption", Color(0xFF312E81), Color(0xFFA78BFA), Challenge.NOISE, Aura.SONIC),
    Species("toxicore", "TOXICORE", "TXC-008", Rarity.RARE, Body.FROG, "Toxic Spit",
        "Spits toxic acid.", "Limited attack range", Color(0xFF0F766E), Color(0xFF67E8F9), Challenge.STEPS, Aura.POISON),
    Species("earthreaper", "EARTHREAPER", "ETR-009", Rarity.EPIC, Body.MOLE, "Ground Slam",
        "Shakes the ground with massive force.", "Exposed crystal core", Color(0xFF3F2A1E), Color(0xFF818CF8), Challenge.CORE, Aura.EARTH),
    Species("overlord", "CRYSTAL OVERLORD", "COV-010", Rarity.LEGENDARY, Body.OVERLORD, "Crystal Dominion",
        "Summons crystal shards and unleashes a nova.", "All nine other DNA signatures combined",
        Color(0xFF2E1065), Color(0xFFF472B6), Challenge.ALL, Aura.PSYCHIC),
    // ---- Mutation Series II–V (organisms 11–50), from the bestiary cards ----
    Species("crystal_serpent", "CRYSTAL SERPENT", "CSS-011", Rarity.EPIC, Body.SERPENT, "Crystal Coil",
        "Binds and constricts.", "Fire", Color(0xFF312E81), Color(0xFFA78BFA), Challenge.PULSE, Aura.CRYSTAL, 2),
    Species("void_jelly", "VOID JELLY", "VJY-012", Rarity.RARE, Body.JELLY, "Void Pulse",
        "Disorients targets.", "Physical damage", Color(0xFF4C1D95), Color(0xFFE879F9), Challenge.CORE, Aura.VOID, 2),
    Species("crystal_mantis", "CRYSTAL MANTIS", "CMT-013", Rarity.UNCOMMON, Body.MANTIS, "Praying Strike",
        "Massive critical hit.", "Speed trap", Color(0xFF14532D), Color(0xFF67E8F9), Challenge.RECOVER, Aura.ELECTRIC, 2),
    Species("cave_golem", "CAVE GOLEM", "CVG-014", Rarity.RARE, Body.GOLEM, "Crystal Smash",
        "Shatters obstacles.", "Energy", Color(0xFF44403C), Color(0xFF93C5FD), Challenge.PULSE, Aura.EARTH, 2),
    Species("spike_crawler", "SPIKE CRAWLER", "SPC-015", Rarity.COMMON, Body.WORM, "Spine Barrage",
        "Shoots spikes.", "Close range", Color(0xFF3F2A3A), Color(0xFFF9A8D4), Challenge.STEPS, Aura.CRYSTAL, 2),
    Species("fungus_beast", "FUNGUS BEAST", "FNB-016", Rarity.EPIC, Body.GORILLA, "Spore Cloud",
        "Blinds and poisons.", "Cleanse", Color(0xFF1A2E05), Color(0xFFC084FC), Challenge.SHAKE, Aura.POISON, 2),
    Species("lumin_wisp", "LUMIN WISP", "LMW-017", Rarity.UNCOMMON, Body.FIREFLY, "Light Drain",
        "Saps energy.", "Darkness", Color(0xFF1E3A8A), Color(0xFF93C5FD), Challenge.DARK, Aura.LIGHT, 2),
    Species("burrow_fiend", "BURROW FIEND", "BRF-018", Rarity.RARE, Body.ARMADILLO, "Burrow Rush",
        "Underground attack.", "Area scan", Color(0xFF3F2A1E), Color(0xFF818CF8), Challenge.SCAN, Aura.EARTH, 2),
    Species("crystal_drone", "CRYSTAL DRONE", "CDR-019", Rarity.EPIC, Body.DRONE, "Drone Swarm",
        "Summons helpers.", "EMP", Color(0xFF1E1B4B), Color(0xFF60A5FA), Challenge.PULSE, Aura.ELECTRIC, 2),
    Species("rift_hive", "THE RIFT HIVE", "RIH-020", Rarity.LEGENDARY, Body.BEETLE, "Rift Eruption",
        "Unleashes a dimensional shockwave.", "Reality anchor", Color(0xFF3B0764), Color(0xFFC084FC), Challenge.STEADY, Aura.VOID, 2),
    Species("crystal_golemite", "CRYSTAL GOLEMITE", "CGM-021", Rarity.EPIC, Body.TURTLE, "Levitation Field",
        "Floats and crushes.", "Gravity trap", Color(0xFF27272A), Color(0xFFA855F7), Challenge.LOW, Aura.PSYCHIC, 3),
    Species("spire_crawler", "SPIRE CRAWLER", "SPC-022", Rarity.RARE, Body.CRAB, "Crystal Spikes",
        "Shoots deadly spikes.", "Close range", Color(0xFF1E293B), Color(0xFFF0ABFC), Challenge.STEPS, Aura.CRYSTAL, 3),
    Species("mind_leech", "MIND LEECH", "MLE-023", Rarity.LEGENDARY, Body.OCTOPUS, "Mind Drain",
        "Drains energy.", "EMP pulse", Color(0xFF701A75), Color(0xFFF472B6), Challenge.PULSE, Aura.PSYCHIC, 3),
    Species("acid_sporer", "ACID SPORER", "ASP-024", Rarity.UNCOMMON, Body.MUSHROOM, "Acid Burst",
        "Spreads corrosive acid.", "Fire", Color(0xFF581C87), Color(0xFFA3E635), Challenge.PULSE, Aura.POISON, 3),
    Species("crystal_stinger", "CRYSTAL STINGER", "CST-025", Rarity.RARE, Body.HORNET, "Paralysis Sting",
        "Paralyses targets.", "Heat", Color(0xFF312E81), Color(0xFFC4B5FD), Challenge.SHAKE, Aura.ELECTRIC, 3),
    Species("void_leviathan", "VOID LEVIATHAN", "VVL-026", Rarity.EPIC, Body.EEL, "Void Wave",
        "Disorients everything nearby.", "Light", Color(0xFF1E1B4B), Color(0xFFE879F9), Challenge.LIGHT, Aura.VOID, 3),
    Species("cave_sapper", "CAVE SAPPER", "CSP-027", Rarity.UNCOMMON, Body.HEDGEHOG, "Burrow Rush",
        "Digs and ambushes.", "Sonar", Color(0xFF18181B), Color(0xFFF472B6), Challenge.NOISE, Aura.EARTH, 3),
    Species("crystal_trapjaw", "CRYSTAL TRAPJAW", "CTJ-028", Rarity.RARE, Body.CROCODILE, "Trap Jaw",
        "Captures prey.", "Ranged attack", Color(0xFF500724), Color(0xFFF472B6), Challenge.RANGED, Aura.CRYSTAL, 3),
    Species("shard_hydra", "SHARD HYDRA", "SHD-029", Rarity.EPIC, Body.HYDRA, "Multi-Strike",
        "Attacks with many heads.", "Decapitation", Color(0xFF3F2A1E), Color(0xFFA78BFA), Challenge.CORE, Aura.FIRE, 3),
    Species("crystal_phantom", "THE CRYSTAL PHANTOM", "CPH-030", Rarity.LEGENDARY, Body.PHOENIX, "Phase Shift",
        "Shifts between realities.", "Pure energy", Color(0xFF1E3A8A), Color(0xFFBAE6FD), Challenge.PULSE, Aura.ICE, 3),
    Species("crystal_bull", "CRYSTAL BULL", "CBL-031", Rarity.EPIC, Body.BULL, "Earth Charge",
        "Tramples targets.", "High ground", Color(0xFF1E1B4B), Color(0xFFA855F7), Challenge.HIGH, Aura.EARTH, 4),
    Species("void_stinger", "VOID STINGER", "VST-032", Rarity.RARE, Body.MOSQUITO, "Void Sting",
        "Drains energy.", "Holy light", Color(0xFF2E1065), Color(0xFFE879F9), Challenge.LIGHT, Aura.VOID, 4),
    Species("crystal_hive", "CRYSTAL HIVE", "CHV-033", Rarity.LEGENDARY, Body.HIVE, "Spawn Swarm",
        "Releases mini drones.", "Area ban", Color(0xFF450A0A), Color(0xFFF472B6), Challenge.SCAN, Aura.FIRE, 4),
    Species("mistwalker", "MISTWALKER", "MWT-034", Rarity.UNCOMMON, Body.DEER, "Phantom Walk",
        "Passes through walls.", "Sound", Color(0xFF334155), Color(0xFFE2E8F0), Challenge.NOISE, Aura.SHADOW, 4),
    Species("spire_squid", "SPIRE SQUID", "SSQ-035", Rarity.EPIC, Body.SQUID, "Ink Cloud",
        "Blinds enemies.", "Electric pulse", Color(0xFF831843), Color(0xFF93C5FD), Challenge.PULSE, Aura.WATER, 4),
    Species("aurora_wyrm", "AURORA WYRM", "AWR-036", Rarity.RARE, Body.LIZARD, "Aurora Breath",
        "Freezes the area.", "Fire", Color(0xFF1E3A8A), Color(0xFFC4B5FD), Challenge.RECOVER, Aura.ICE, 4),
    Species("stone_larva", "STONE LARVA", "STL-037", Rarity.UNCOMMON, Body.CATERPILLAR, "Burrow Burst",
        "Surges from the ground.", "Gas", Color(0xFF292524), Color(0xFFA78BFA), Challenge.SHAKE, Aura.EARTH, 4),
    Species("crystal_gargoyle", "CRYSTAL GARGOYLE", "CGA-038", Rarity.LEGENDARY, Body.EAGLE, "Sky Dive",
        "Drops from above.", "EMP pulse", Color(0xFF1C1917), Color(0xFFA78BFA), Challenge.HIGH, Aura.SHADOW, 4),
    Species("rift_bloom", "RIFT BLOOM", "RFB-039", Rarity.EPIC, Body.ANEMONE, "Spore Release",
        "Poisons nearby.", "Cold", Color(0xFF701A75), Color(0xFFE879F9), Challenge.STEADY, Aura.POISON, 4),
    Species("crystal_colossus", "THE CRYSTAL COLOSSUS", "CRC-040", Rarity.LEGENDARY, Body.ELEPHANT, "Crystal Nova",
        "Massive area blast.", "Energy drain", Color(0xFF0F172A), Color(0xFF818CF8), Challenge.CORE, Aura.CRYSTAL, 4),
    Species("crystal_titan", "CRYSTAL TITAN", "CTN-041", Rarity.EPIC, Body.RHINO, "Crystal Rampage",
        "Area smash.", "Energy discharge", Color(0xFF1E1B4B), Color(0xFFC084FC), Challenge.PULSE, Aura.CRYSTAL, 5),
    Species("shadow_reef", "SHADOW REEF", "SHR-042", Rarity.RARE, Body.MANTA, "Shadow Glide",
        "Becomes invisible.", "Light", Color(0xFF1E1B4B), Color(0xFF818CF8), Challenge.LIGHT, Aura.SHADOW, 5),
    Species("bone_crawler", "BONE CRAWLER", "BCR-043", Rarity.UNCOMMON, Body.ANT, "Bone Drill",
        "Burrows fast.", "Shock", Color(0xFF57534E), Color(0xFFE7E5E4), Challenge.SHAKE, Aura.EARTH, 5),
    Species("crystal_egg", "CRYSTAL EGG", "CEG-044", Rarity.RARE, Body.EGG, "Hatch Swarm",
        "Spawns minions.", "Fire", Color(0xFF3B0764), Color(0xFFFBBF24), Challenge.CORE, Aura.FIRE, 5),
    Species("neon_jelly", "NEON JELLY", "NJL-045", Rarity.EPIC, Body.ANGLER, "Electric Pulse",
        "Paralyses.", "EMP", Color(0xFF1E3A8A), Color(0xFF67E8F9), Challenge.PULSE, Aura.ELECTRIC, 5),
    Species("crystal_plant", "CRYSTAL PLANT", "CPT-046", Rarity.UNCOMMON, Body.PLANT, "Thorn Burst",
        "Spike attack.", "Ice", Color(0xFF14532D), Color(0xFFE879F9), Challenge.STEADY, Aura.POISON, 5),
    Species("gravity_sphere", "GRAVITY SPHERE", "GRS-047", Rarity.RARE, Body.ORB, "Gravity Field",
        "Pulls everything nearby.", "Disruption", Color(0xFF1E1B4B), Color(0xFFA78BFA), Challenge.SHAKE, Aura.PSYCHIC, 5),
    Species("sand_specter", "SAND SPECTER", "SNS-048", Rarity.LEGENDARY, Body.WRAITH, "Sandstorm",
        "Blinds the area.", "Water", Color(0xFF44403C), Color(0xFFFDE68A), Challenge.LOW, Aura.EARTH, 5),
    Species("crystal_leviathan", "CRYSTAL LEVIATHAN", "CLV-049", Rarity.EPIC, Body.SHARK, "Tidal Surge",
        "Wave attack.", "Heat", Color(0xFF172554), Color(0xFF93C5FD), Challenge.SHAKE, Aura.WATER, 5),
    Species("veilcore", "THE VEILCORE", "VEC-050", Rarity.LEGENDARY, Body.STARFISH, "Reality Rift",
        "Phase shift.", "Pure light", Color(0xFF2E1065), Color(0xFFC4B5FD), Challenge.LIGHT, Aura.PSYCHIC, 5),
)

private fun DrawScope.crystals(at: Offset, s: Float, c: Color, n: Int, spread: Float, t: Float) {
    for (i in 0 until n) {
        val f = if (n == 1) 0f else i / (n - 1f) - 0.5f
        val base = at + Offset(f * spread, 0f)
        val h = s * (0.55f + 0.35f * ((i * 37 % 10) / 10f)) * (1f + 0.06f * sin(t * 3 + i))
        val w = s * 0.16f
        val p = Path().apply { moveTo(base.x - w, base.y); lineTo(base.x + f * w * 2, base.y - h); lineTo(base.x + w, base.y); close() }
        drawPath(p, Brush.verticalGradient(listOf(Color.White.copy(alpha = 0.9f), c, c.copy(alpha = 0.6f)), base.y - h, base.y))
    }
}

private fun DrawScope.legs(c: Offset, s: Float, n: Int, len: Float, col: Color, t: Float, span: Float = 0.9f) {
    for (side in listOf(-1f, 1f)) for (i in 0 until n) {
        val a = (-span / 2 + span * i / (n - 1f)) + sin(t * 9 + i * 1.7f) * 0.12f
        val knee = c + Offset(side * cos(a) * len * 0.6f, -s * 0.15f + sin(a) * len * 0.5f)
        val foot = c + Offset(side * cos(a) * len, s * 0.35f + sin(a) * len * 0.6f)
        drawLine(col, c, knee, strokeWidth = s * 0.06f, cap = StrokeCap.Round)
        drawLine(col, knee, foot, strokeWidth = s * 0.05f, cap = StrokeCap.Round)
    }
}

/** Power-specific aura behind a creature. */
private fun DrawScope.drawAura(a: Aura, c: Offset, s: Float, t: Float, glow: Color) {
    val r = s * 1.9f
    fun halo(col: Color, alpha: Float = 0.35f) = drawCircle(Brush.radialGradient(listOf(col.copy(alpha = alpha), Color.Transparent), c, r), r, c)
    when (a) {
        Aura.FIRE -> { halo(Color(0xFFF97316)); for (k in 0 until 9) { val x = c.x + (k - 4) * s * 0.28f; val h = s * (0.6f + 0.4f * sin(t * 9 + k * 1.7f))
            drawOval(Brush.verticalGradient(listOf(Color(0xFFFDE047).copy(alpha = 0.7f), Color(0xFFEF4444).copy(alpha = 0.0f)), c.y + s * 0.8f - h, c.y + s * 0.9f),
                Offset(x - s * 0.12f, c.y + s * 0.9f - h), Size(s * 0.24f, h)) } }
        Aura.ICE -> { halo(Color(0xFF7DD3FC)); for (k in 0 until 6) rotate(k * 60f + t * 10f, c) {
            drawLine(Color(0xFFE0F2FE).copy(alpha = 0.6f), c + Offset(0f, -s * 1.3f), c + Offset(0f, -s * 1.75f), s * 0.05f, StrokeCap.Round)
            drawLine(Color(0xFFE0F2FE).copy(alpha = 0.5f), c + Offset(0f, -s * 1.55f), c + Offset(s * 0.12f, -s * 1.68f), s * 0.03f) } }
        Aura.ELECTRIC -> { halo(Color(0xFF38BDF8), 0.25f); val seed = (t * 12).toInt()
            for (k in 0 until 4) { val a0 = k * 1.57f + seed * 0.7f; var p = c + Offset(cos(a0) * s * 0.8f, sin(a0) * s * 0.8f)
                repeat(4) { val n = p + Offset(cos(a0 + (it % 2) * 0.9f - 0.45f) * s * 0.3f, sin(a0 + (it % 2) * 0.9f - 0.45f) * s * 0.3f); drawLine(Color(0xFFE0F2FE), p, n, s * 0.035f); p = n } } }
        Aura.VOID -> { drawCircle(Brush.radialGradient(listOf(Color.Black.copy(alpha = 0.7f), Color(0xFF7C3AED).copy(alpha = 0.35f), Color.Transparent), c, r), r, c)
            for (k in 0 until 3) drawArc(Color(0xFFC084FC).copy(alpha = 0.5f), t * 90f + k * 120f, 70f, false, c - Offset(s * 1.4f, s * 1.4f), Size(s * 2.8f, s * 2.8f), style = Stroke(s * 0.05f, cap = StrokeCap.Round)) }
        Aura.POISON -> { halo(Color(0xFF84CC16), 0.28f); for (k in 0 until 8) { val ph = (t * 0.6f + k / 8f) % 1f
            drawCircle(Color(0xFFA3E635).copy(alpha = 0.6f * (1f - ph)), s * (0.08f + 0.06f * (k % 3)), c + Offset((k - 3.5f) * s * 0.3f, s * 0.8f - ph * s * 2.2f)) } }
        Aura.LIGHT -> { halo(Color(0xFFFDE047), 0.3f); for (k in 0 until 12) rotate(k * 30f + t * 15f, c) {
            drawLine(Color(0xFFFEF9C3).copy(alpha = 0.35f + 0.25f * sin(t * 4 + k)), c + Offset(0f, -s * 1.1f), c + Offset(0f, -s * 1.9f), s * 0.06f, StrokeCap.Round) } }
        Aura.EARTH -> { halo(Color(0xFFB45309), 0.25f); for (k in 0 until 5) { val a1 = t * 1.2f + k * 1.256f
            drawRect(Color(0xFF78716C), c + Offset(cos(a1) * s * 1.4f - s * 0.1f, sin(a1) * s * 0.6f + s * 0.4f - s * 0.1f), Size(s * 0.2f, s * 0.2f)) } }
        Aura.SONIC -> { halo(glow, 0.2f); for (k in 0 until 3) { val ph = (t * 0.8f + k / 3f) % 1f
            drawCircle(Color(0xFFC4B5FD).copy(alpha = 0.6f * (1f - ph)), s * (0.9f + ph * 1.3f), c, style = Stroke(s * 0.04f)) } }
        Aura.PSYCHIC -> { halo(Color(0xFFF472B6), 0.3f); for (k in 0 until 4) { val ph = (t * 0.5f + k / 4f) % 1f
            drawOval(Color(0xFFF9A8D4).copy(alpha = 0.5f * (1f - ph)), Offset(c.x - s * (1f + ph), c.y - s * (0.5f + ph * 0.5f)), Size(s * (2f + ph * 2f), s * (1f + ph)), style = Stroke(s * 0.035f)) } }
        Aura.WATER -> { halo(Color(0xFF0EA5E9), 0.3f); for (k in 0 until 3) { val ph = (t * 0.7f + k / 3f) % 1f
            drawOval(Color(0xFF7DD3FC).copy(alpha = 0.6f * (1f - ph)), Offset(c.x - s * (0.8f + ph * 1.2f), c.y + s * 0.8f - s * 0.12f), Size(s * (1.6f + ph * 2.4f), s * 0.24f), style = Stroke(s * 0.04f)) } }
        Aura.SHADOW -> { for (k in 0 until 7) { val a2 = k * 0.9f + t * 0.8f
            drawCircle(Color.Black.copy(alpha = 0.35f), s * (0.6f + 0.2f * sin(t * 2 + k)), c + Offset(cos(a2) * s * 0.8f, sin(a2) * s * 0.5f)) }
            halo(Color(0xFF6366F1), 0.18f) }
        Aura.CRYSTAL -> { halo(glow); for (k in 0 until 5) { val a3 = t * 0.9f + k * 1.256f; val p = c + Offset(cos(a3) * s * 1.5f, sin(a3) * s * 0.7f)
            val path = Path().apply { moveTo(p.x, p.y - s * 0.18f); lineTo(p.x + s * 0.08f, p.y); lineTo(p.x, p.y + s * 0.18f); lineTo(p.x - s * 0.08f, p.y); close() }
            drawPath(path, glow.copy(alpha = 0.8f)) } }
    }
}

/** Draws one species centred at [c], [s] ≈ half its height in px. [t] = time (s) for idle animation. */
fun DrawScope.drawSpecies(sp: Species, c: Offset, s: Float, t: Float, facingLeft: Boolean = false) {
    val glow = sp.crystal
    drawAura(sp.aura, c, s, t, glow)
    val body = sp.color
    val dark = Color(0xFF0B0F1A)
    val flip = if (facingLeft) -1f else 1f
    when (sp.body) {
        Body.SPIDER, Body.OVERLORD -> {
            val k = if (sp.body == Body.OVERLORD) 1.3f else 1f
            legs(c, s * k, 4, s * 1.25f * k, dark.copy(alpha = 0.95f), t)
            legs(c, s * k, 4, s * 1.2f * k, glow.copy(alpha = 0.55f), t + 0.3f)
            drawOval(Brush.radialGradient(listOf(body.copy(alpha = 1f), dark), c, s), Offset(c.x - s * 0.7f * k, c.y - s * 0.45f * k), Size(s * 1.4f * k, s * 0.95f * k))
            drawCircle(body, s * 0.35f * k, c + Offset(flip * s * 0.75f * k, s * 0.05f))
            crystals(c + Offset(0f, -s * 0.3f * k), s * k, glow, if (sp.body == Body.OVERLORD) 7 else 5, s * 0.9f * k, t)
            val eye = if (sp.body == Body.OVERLORD) Color(0xFFF0ABFC) else Color(0xFFEF4444)
            drawCircle(eye, s * 0.07f * k, c + Offset(flip * s * 0.95f * k, -s * 0.02f))
            drawCircle(eye, s * 0.07f * k, c + Offset(flip * s * 0.82f * k, s * 0.12f))
            if (sp.body == Body.OVERLORD) {   // crystal blade arms + crown aura
                for (side in listOf(-1f, 1f)) rotate(side * (35f + 8f * sin(t * 4)), c) {
                    drawLine(glow, c, c + Offset(side * s * 1.9f, -s * 1.1f), s * 0.12f, StrokeCap.Round)
                }
                drawCircle(glow.copy(alpha = 0.5f), s * (1.7f + 0.1f * sin(t * 2)), c, style = Stroke(s * 0.05f))
            }
        }
        Body.CENTIPEDE -> {
            for (i in 0 until 9) {
                val x = c.x + flip * (i - 4) * s * 0.32f
                val y = c.y + sin(t * 6 + i * 0.8f) * s * 0.12f
                drawLine(dark, Offset(x, y), Offset(x, y + s * 0.45f), s * 0.05f, StrokeCap.Round)
                drawCircle(Brush.radialGradient(listOf(body, dark), Offset(x, y), s * 0.3f), s * 0.24f, Offset(x, y))
                if (i % 2 == 0) crystals(Offset(x, y - s * 0.15f), s * 0.5f, glow, 1, 0f, t + i)
            }
            drawCircle(Color(0xFFFBBF24), s * 0.06f, c + Offset(flip * s * 1.42f, -s * 0.05f))
        }
        Body.MOTH -> {
            val flap = 0.55f + 0.45f * sin(t * 10)
            for (side in listOf(-1f, 1f)) {
                val p = Path().apply {
                    moveTo(c.x, c.y)
                    cubicTo(c.x + side * s * 1.6f * flap, c.y - s * 1.5f, c.x + side * s * 2.0f * flap, c.y + s * 0.3f, c.x, c.y + s * 0.2f)
                }
                drawPath(p, Brush.radialGradient(listOf(Color.White.copy(alpha = 0.8f), glow, body.copy(alpha = 0.7f)), c, s * 2f))
                drawPath(p, Color.White.copy(alpha = 0.6f), style = Stroke(s * 0.03f))
            }
            drawOval(dark, Offset(c.x - s * 0.1f, c.y - s * 0.45f), Size(s * 0.2f, s * 1.0f))
        }
        Body.RAT, Body.MOLE -> {
            val k = if (sp.body == Body.MOLE) 1.2f else 1f
            val step = sin(t * 8) * s * 0.08f
            drawLine(Color(0xFFCBD5E1).copy(alpha = 0.7f), c + Offset(-flip * s * 0.9f * k, s * 0.1f),
                c + Offset(-flip * s * 1.8f * k, s * (0.3f + 0.2f * sin(t * 3))), s * 0.06f, StrokeCap.Round)
            drawOval(Brush.radialGradient(listOf(body.copy(alpha = 1f), dark), c, s * 1.2f * k),
                Offset(c.x - s * 1.0f * k, c.y - s * 0.55f * k), Size(s * 2.0f * k, s * 1.1f * k))
            drawCircle(body, s * 0.42f * k, c + Offset(flip * s * 1.0f * k, s * 0.05f))
            drawCircle(Color(0xFFEF4444), s * 0.07f, c + Offset(flip * s * 1.15f * k, -s * 0.08f))
            for (dx in listOf(-0.5f, 0.6f)) drawLine(dark, c + Offset(dx * s * k, s * 0.4f * k), c + Offset(dx * s * k + step, s * 0.75f * k), s * 0.1f, StrokeCap.Round)
            if (sp.body == Body.MOLE) for (i in 0 until 3) drawLine(Color(0xFFE5E7EB), c + Offset(flip * s * 1.2f, s * 0.3f),
                c + Offset(flip * s * (1.55f + i * 0.08f), s * (0.55f + i * 0.12f)), s * 0.06f, StrokeCap.Round)
            crystals(c + Offset(-flip * s * 0.1f, -s * 0.4f * k), s * k, glow, 5, s * 1.2f * k, t)
        }
        Body.SCORPION -> {
            legs(c, s, 4, s * 1.0f, dark, t, 0.7f)
            for (i in 0 until 4) drawCircle(body, s * (0.4f - i * 0.05f), c + Offset(-flip * s * (0.3f + i * 0.35f), 0f))
            // tail curls up and over the back; the stinger sways
            val tail = listOf(Offset(1.4f, -0.4f), Offset(1.55f, -1.0f), Offset(1.2f, -1.5f), Offset(0.6f, -1.55f + 0.15f * sin(t * 5)))
            tail.forEach { o -> drawCircle(body, s * 0.2f, c + Offset(-flip * o.x * s, o.y * s)) }
            drawCircle(glow, s * 0.16f, c + Offset(-flip * 0.3f * s, (-1.6f + 0.15f * sin(t * 5)) * s))
            for (side in listOf(-1f, 1f)) drawCircle(glow.copy(alpha = 0.8f), s * 0.18f, c + Offset(flip * s * 1.1f, side * s * 0.35f))
        }
        Body.BAT -> {
            val flap = 0.6f + 0.4f * sin(t * 12)
            for (side in listOf(-1f, 1f)) {
                val p = Path().apply {
                    moveTo(c.x, c.y)
                    lineTo(c.x + side * s * 1.0f, c.y - s * 0.9f * flap)
                    lineTo(c.x + side * s * 2.0f, c.y - s * 0.4f * flap)
                    lineTo(c.x + side * s * 1.6f, c.y + s * 0.2f)
                    lineTo(c.x + side * s * 1.1f, c.y)
                    lineTo(c.x + side * s * 0.6f, c.y + s * 0.3f)
                    close()
                }
                drawPath(p, Brush.radialGradient(listOf(glow, body.copy(alpha = 0.8f)), c, s * 2f))
            }
            drawOval(dark, Offset(c.x - s * 0.25f, c.y - s * 0.35f), Size(s * 0.5f, s * 0.75f))
            drawCircle(Color(0xFFF0ABFC), s * 0.05f, c + Offset(-s * 0.08f, -s * 0.15f))
            drawCircle(Color(0xFFF0ABFC), s * 0.05f, c + Offset(s * 0.08f, -s * 0.15f))
        }
        Body.FROG -> {
            val hop = -kotlin.math.abs(sin(t * 3)) * s * 0.35f
            val cc = c + Offset(0f, hop)
            for (side in listOf(-1f, 1f)) drawOval(Color(0xFFF59E0B).copy(alpha = 0.9f), Offset(cc.x + side * s * 0.6f - s * 0.35f, cc.y + s * 0.2f), Size(s * 0.7f, s * 0.4f))
            drawOval(Brush.radialGradient(listOf(Color(0xFF34D399), body), cc, s * 1.1f), Offset(cc.x - s * 0.9f, cc.y - s * 0.6f), Size(s * 1.8f, s * 1.1f))
            for (side in listOf(-1f, 1f)) {
                drawCircle(Color(0xFFFDE68A), s * 0.2f, cc + Offset(side * s * 0.45f, -s * 0.55f))
                drawCircle(dark, s * 0.09f, cc + Offset(side * s * 0.45f, -s * 0.55f))
            }
            crystals(cc + Offset(0f, -s * 0.45f), s * 0.8f, glow, 4, s * 0.8f, t)
        }
        Body.SERPENT -> {   // coiled crystal serpent / wyrm / leviathan
            for (i in 0 until 14) {
                val f = i / 13f
                val x = c.x - flip * (f - 0.45f) * s * 2.4f
                val y = c.y + sin(t * 3 + f * 7) * s * 0.35f + f * s * 0.3f
                val r = s * (0.42f - f * 0.25f)
                drawCircle(Brush.radialGradient(listOf(body, dark), Offset(x, y), r * 1.3f), r, Offset(x, y))
                if (i % 3 == 1) crystals(Offset(x, y - r * 0.6f), s * 0.45f, glow, 1, 0f, t + i)
            }
            val hx = c.x + flip * s * 1.15f; val hy = c.y - s * 0.15f + sin(t * 3) * s * 0.35f
            drawOval(body, Offset(hx - s * 0.45f, hy - s * 0.3f), Size(s * 0.9f, s * 0.6f))
            drawCircle(Color(0xFFFDE047), s * 0.07f, Offset(hx + flip * s * 0.15f, hy - s * 0.1f))
            drawLine(Color(0xFFF43F5E), Offset(hx + flip * s * 0.45f, hy + s * 0.05f), Offset(hx + flip * s * (0.7f + 0.1f * sin(t * 12)), hy + s * 0.12f), s * 0.04f, StrokeCap.Round)
        }
        Body.JELLY -> {     // jellyfish / mind leech: dome + drifting tentacles
            val bob = sin(t * 2) * s * 0.15f
            val cc = c + Offset(0f, bob - s * 0.3f)
            for (i in 0 until 7) {
                val x0 = cc.x + (i - 3) * s * 0.2f
                val path = Path().apply {
                    moveTo(x0, cc.y)
                    for (k in 1..6) lineTo(x0 + sin(t * 3 + i + k * 0.9f) * s * 0.15f, cc.y + k * s * 0.28f)
                }
                drawPath(path, glow.copy(alpha = 0.7f), style = Stroke(s * 0.05f, cap = StrokeCap.Round))
            }
            drawArc(Brush.radialGradient(listOf(Color.White.copy(alpha = 0.7f), glow, body), cc, s), 180f, 180f, true,
                Offset(cc.x - s * 0.8f, cc.y - s * 0.75f), Size(s * 1.6f, s * 1.5f))
            drawCircle(Color.White.copy(alpha = 0.5f + 0.4f * sin(t * 5)), s * 0.14f, cc + Offset(0f, -s * 0.3f))
        }
        Body.MANTIS -> {    // mantis / stinger: thin body, crystal wings, scythe arms
            val flap = 0.6f + 0.4f * sin(t * 14)
            for (side in listOf(-1f, 1f)) {
                val wing = Path().apply { moveTo(c.x, c.y - s * 0.2f); quadraticBezierTo(c.x + side * s * 1.4f * flap, c.y - s * 1.3f, c.x + side * s * 0.3f, c.y + s * 0.1f); close() }
                drawPath(wing, glow.copy(alpha = 0.45f))
                drawPath(wing, Color.White.copy(alpha = 0.5f), style = Stroke(s * 0.025f))
            }
            drawLine(body, c + Offset(-flip * s * 0.9f, s * 0.3f), c + Offset(flip * s * 0.4f, -s * 0.2f), s * 0.22f, StrokeCap.Round)
            drawCircle(body, s * 0.2f, c + Offset(flip * s * 0.6f, -s * 0.45f))
            drawCircle(Color(0xFFA3E635), s * 0.06f, c + Offset(flip * s * 0.7f, -s * 0.5f))
            val strike = sin(t * 4).coerceAtLeast(0f)
            drawLine(glow, c + Offset(flip * s * 0.4f, -s * 0.1f), c + Offset(flip * s * (0.95f + strike * 0.3f), -s * (0.9f - strike * 0.5f)), s * 0.08f, StrokeCap.Round)
            for (side in listOf(-1f, 1f)) drawLine(dark, c + Offset(side * s * 0.2f, s * 0.15f), c + Offset(side * s * 0.6f, s * 0.75f), s * 0.05f, StrokeCap.Round)
        }
        Body.GOLEM -> {     // golem / titan / colossus: stacked rock blocks with a glowing core
            val sway = sin(t * 2) * s * 0.06f
            for ((dx, dy, w) in listOf(Triple(0f, -0.15f, 1.0f), Triple(-0.75f, -0.05f, 0.45f), Triple(0.75f, -0.05f, 0.45f), Triple(-0.35f, 0.75f, 0.35f), Triple(0.35f, 0.75f, 0.35f), Triple(0f, -0.95f, 0.45f)))
                drawRoundRect(Brush.verticalGradient(listOf(body.copy(alpha = 1f), dark), c.y + dy * s - w * s * 0.5f, c.y + dy * s + w * s * 0.5f),
                    Offset(c.x + dx * s - w * s * 0.5f + sway, c.y + dy * s - w * s * 0.5f), Size(w * s, w * s), androidx.compose.ui.geometry.CornerRadius(s * 0.12f))
            drawCircle(Brush.radialGradient(listOf(Color.White, glow, Color.Transparent), c + Offset(sway, -s * 0.15f), s * 0.35f), s * 0.35f, c + Offset(sway, -s * 0.15f))
            crystals(c + Offset(sway, -s * 1.1f), s * 0.9f, glow, 5, s * 1.1f, t)
            drawCircle(glow, s * 0.06f, c + Offset(sway - s * 0.1f, -s * 0.98f)); drawCircle(glow, s * 0.06f, c + Offset(sway + s * 0.1f, -s * 0.98f))
        }
        Body.WORM -> {      // spike / bone / stone crawler: arching segmented worm
            for (i in 0 until 10) {
                val f = i / 9f
                val x = c.x + flip * (f - 0.5f) * s * 2.2f
                val y = c.y + s * 0.4f - sin(f * 3.14f) * s * (0.8f + 0.15f * sin(t * 4))
                drawCircle(Brush.radialGradient(listOf(body, dark), Offset(x, y), s * 0.4f), s * (0.34f - f * 0.06f), Offset(x, y))
                drawLine(glow.copy(alpha = 0.8f), Offset(x, y - s * 0.3f), Offset(x, y - s * 0.55f), s * 0.05f, StrokeCap.Round)
            }
            val hx = c.x + flip * s * 1.15f
            drawCircle(dark, s * 0.18f, Offset(hx, c.y + s * 0.35f))
            for (k in 0 until 5) { val a = k * 1.25f + t * 2; drawLine(Color(0xFFE5E7EB), Offset(hx, c.y + s * 0.35f), Offset(hx + cos(a) * s * 0.25f, c.y + s * 0.35f + sin(a) * s * 0.25f), s * 0.03f) }
        }
        Body.PLANT -> {     // carnivorous crystal plant / sporer / bloom
            for (side in listOf(-1f, 1f)) drawLine(Color(0xFF15803D), c + Offset(0f, s * 0.9f), c + Offset(side * s * 0.9f, s * (0.5f + 0.1f * sin(t * 2 + side))), s * 0.08f, StrokeCap.Round)
            drawLine(Color(0xFF166534), c + Offset(0f, s * 0.95f), c + Offset(sin(t * 1.5f) * s * 0.2f, -s * 0.1f), s * 0.12f, StrokeCap.Round)
            val head = c + Offset(sin(t * 1.5f) * s * 0.2f, -s * 0.3f)
            for (k in 0 until 8) rotate(k * 45f + t * 20f, head) {
                drawOval(Brush.verticalGradient(listOf(glow, body), head.y - s * 0.95f, head.y), Offset(head.x - s * 0.18f, head.y - s * 0.95f), Size(s * 0.36f, s * 0.7f))
            }
            drawCircle(dark, s * 0.32f, head)
            val bite = 0.5f + 0.5f * sin(t * 5)
            drawArc(Color(0xFFF43F5E), 20f, 140f * bite, false, Offset(head.x - s * 0.2f, head.y - s * 0.2f), Size(s * 0.4f, s * 0.4f), style = Stroke(s * 0.05f))
        }
        Body.ORB -> {       // egg / sphere / drone / hive / veilcore: a core with orbiting shards
            drawCircle(Brush.radialGradient(listOf(Color.White.copy(alpha = 0.9f), glow, body, dark), c, s * 0.75f), s * 0.7f, c)
            for (k in 0 until 6) {
                val a = t * 1.6f + k * 1.047f
                val p = c + Offset(cos(a) * s * 1.15f, sin(a) * s * 0.45f)
                val path = Path().apply { moveTo(p.x, p.y - s * 0.22f); lineTo(p.x + s * 0.1f, p.y); lineTo(p.x, p.y + s * 0.22f); lineTo(p.x - s * 0.1f, p.y); close() }
                drawPath(path, glow)
            }
            drawOval(glow.copy(alpha = 0.6f), Offset(c.x - s * 1.2f, c.y - s * 0.12f), Size(s * 2.4f, s * 0.24f), style = Stroke(s * 0.04f))
            drawCircle(Color.White, s * (0.12f + 0.04f * sin(t * 6)), c)
        }
        Body.SQUID -> {     // spire squid: crystal mantle + curling arms
            for (i in 0 until 8) {
                val x0 = c.x + (i - 3.5f) * s * 0.16f
                val path = Path().apply { moveTo(x0, c.y); for (k in 1..7) lineTo(x0 + sin(t * 2.5f + i * 0.7f + k * 0.8f) * s * 0.22f * k / 7f, c.y + k * s * 0.17f) }
                drawPath(path, body.copy(alpha = 0.95f), style = Stroke(s * 0.08f, cap = StrokeCap.Round))
            }
            val m = Path().apply { moveTo(c.x - s * 0.55f, c.y); quadraticBezierTo(c.x, c.y - s * 2.2f, c.x + s * 0.55f, c.y); close() }
            drawPath(m, Brush.verticalGradient(listOf(glow, body), c.y - s * 1.6f, c.y))
            drawCircle(Color(0xFFFDE68A), s * 0.1f, c + Offset(-s * 0.2f, -s * 0.2f)); drawCircle(Color(0xFFFDE68A), s * 0.1f, c + Offset(s * 0.2f, -s * 0.2f))
        }
        Body.WRAITH -> {    // specter / wisp / phantom / mistwalker: flowing translucent form
            val bob = sin(t * 2.2f) * s * 0.18f
            val cc = c + Offset(0f, bob)
            val cloak = Path().apply {
                moveTo(cc.x, cc.y - s * 1.1f)
                cubicTo(cc.x + s * 1.0f, cc.y - s * 0.8f, cc.x + s * 0.9f, cc.y + s * 0.6f, cc.x + s * 0.7f, cc.y + s * 1.1f)
                for (k in 0..6) lineTo(cc.x + s * (0.7f - k * 0.233f), cc.y + s * (1.1f - 0.25f * ((k + (t * 3).toInt()) % 2)))
                cubicTo(cc.x - s * 0.9f, cc.y + s * 0.6f, cc.x - s * 1.0f, cc.y - s * 0.8f, cc.x, cc.y - s * 1.1f)
            }
            drawPath(cloak, Brush.verticalGradient(listOf(glow.copy(alpha = 0.85f), body.copy(alpha = 0.5f), Color.Transparent), cc.y - s * 1.1f, cc.y + s * 1.2f))
            drawCircle(Color.White, s * 0.09f, cc + Offset(-s * 0.2f, -s * 0.55f)); drawCircle(Color.White, s * 0.09f, cc + Offset(s * 0.2f, -s * 0.55f))
            drawCircle(glow, s * (0.15f + 0.05f * sin(t * 6)), cc + Offset(0f, -s * 0.1f))
        }
        Body.HYDRA -> {     // shard hydra: one body, three swaying necks
            drawOval(Brush.radialGradient(listOf(body, dark), c + Offset(0f, s * 0.4f), s), Offset(c.x - s * 0.9f, c.y + s * 0.05f), Size(s * 1.8f, s * 0.8f))
            for (k in -1..1) {
                val sway = sin(t * 3 + k * 1.3f) * s * 0.25f
                val head = c + Offset(k * s * 0.7f + sway, -s * (1.0f + 0.15f * (1 - abs(k))))
                drawLine(body, c + Offset(k * s * 0.35f, s * 0.2f), head, s * 0.2f, StrokeCap.Round)
                drawOval(body, Offset(head.x - s * 0.26f, head.y - s * 0.18f), Size(s * 0.52f, s * 0.36f))
                drawCircle(Color(0xFFFDE047), s * 0.05f, head + Offset(s * 0.1f, -s * 0.05f))
                crystals(head + Offset(0f, -s * 0.12f), s * 0.35f, glow, 3, s * 0.3f, t + k)
            }
        }
        Body.BULL -> {      // crystal bull / mistwalker: four legs, horns, charging
            val step = sin(t * 7) * s * 0.1f
            for ((dx, ph) in listOf(-0.6f to 0f, -0.3f to 1f, 0.35f to 0.5f, 0.65f to 1.5f))
                drawLine(dark, c + Offset(dx * s, s * 0.3f), c + Offset(dx * s + step * sin(ph), s * 0.95f), s * 0.14f, StrokeCap.Round)
            drawOval(Brush.radialGradient(listOf(body, dark), c, s * 1.2f), Offset(c.x - s * 0.95f, c.y - s * 0.4f), Size(s * 1.9f, s * 0.9f))
            val head = c + Offset(flip * s * 1.0f, -s * 0.15f)
            drawCircle(body, s * 0.35f, head)
            for (side in listOf(-1f, 1f)) drawLine(glow, head + Offset(0f, -s * 0.2f), head + Offset(flip * s * 0.35f, -s * (0.55f + side * 0.12f)), s * 0.09f, StrokeCap.Round)
            drawCircle(Color(0xFFF43F5E), s * 0.06f, head + Offset(flip * s * 0.12f, -s * 0.05f))
            crystals(c + Offset(0f, -s * 0.35f), s * 0.8f, glow, 5, s * 1.2f, t)
        }
        else -> drawExtraBody(sp, c, s, t, flip, body, glow, dark)
    }
}

fun speciesById(id: String?) = SPECIES.firstOrNull { it.id == id }
fun twoPi() = (2 * PI).toFloat()

private fun eye(scope: DrawScope, p: Offset, r: Float, col: Color = Color(0xFFFDE047)) {
    scope.drawCircle(col, r, p); scope.drawCircle(Color(0xFF0B0F1A), r * 0.5f, p)
}

/** The 30 real-world-inspired body plans (land, water, sky, underground) — so no two species look alike. */
private fun DrawScope.drawExtraBody(sp: Species, c: Offset, s: Float, t: Float, flip: Float, body: Color, glow: Color, dark: Color) {
    val d = this
    when (sp.body) {
        Body.WEBSPINNER -> {   // spider hanging on a silk thread in front of its web
            for (k in 0 until 8) rotate(k * 45f, c + Offset(0f, -s * 0.2f)) { drawLine(Color.White.copy(alpha = 0.25f), c + Offset(0f, -s * 0.2f), c + Offset(0f, -s * 1.8f), 2f) }
            for (r in 1..3) drawCircle(Color.White.copy(alpha = 0.18f), s * 0.5f * r, c + Offset(0f, -s * 0.2f), style = Stroke(2f))
            val y = c.y + sin(t * 2) * s * 0.25f
            drawLine(Color.White.copy(alpha = 0.6f), Offset(c.x, c.y - s * 1.9f), Offset(c.x, y - s * 0.3f), 2.5f)
            legs(Offset(c.x, y), s * 0.8f, 4, s * 1.2f, dark, t)
            drawCircle(Brush.radialGradient(listOf(body, dark), Offset(c.x, y + s * 0.2f), s * 0.5f), s * 0.5f, Offset(c.x, y + s * 0.2f))
            drawCircle(Color(0xFFEF4444), s * 0.12f, Offset(c.x, y + s * 0.25f))   // hourglass mark
            drawCircle(body, s * 0.25f, Offset(c.x, y - s * 0.3f))
        }
        Body.CRAB -> {   // sideways crab with big pincers
            val walk = sin(t * 6) * s * 0.08f
            for (side in listOf(-1f, 1f)) for (i in 0 until 3) drawLine(dark, c + Offset(side * s * 0.5f, s * 0.1f), c + Offset(side * s * (1.0f + i * 0.15f) + walk, s * (0.45f + i * 0.12f)), s * 0.07f, StrokeCap.Round)
            drawOval(Brush.radialGradient(listOf(Color(0xFFF97316), body), c, s), Offset(c.x - s * 0.8f, c.y - s * 0.45f), Size(s * 1.6f, s * 0.9f))
            for (side in listOf(-1f, 1f)) {
                val claw = c + Offset(side * s * 1.15f, -s * 0.6f + sin(t * 3 + side) * s * 0.1f)
                drawLine(Color(0xFFEA580C), c + Offset(side * s * 0.6f, -s * 0.2f), claw, s * 0.1f, StrokeCap.Round)
                drawArc(Color(0xFFEA580C), if (side > 0) 200f else -20f, 140f, true, Offset(claw.x - s * 0.3f, claw.y - s * 0.3f), Size(s * 0.6f, s * 0.6f))
            }
            eye(d, c + Offset(-s * 0.2f, -s * 0.55f), s * 0.09f); eye(d, c + Offset(s * 0.2f, -s * 0.55f), s * 0.09f)
            crystals(c + Offset(0f, -s * 0.35f), s * 0.9f, glow, 1, 0f, t)
        }
        Body.ARMADILLO -> {   // banded armour shell, small head and tail
            for (i in 0 until 6) {
                val x = c.x + (i - 2.5f) * s * 0.3f
                drawArc(Brush.verticalGradient(listOf(Color(0xFFD6D3D1), body), c.y - s * 0.8f, c.y + s * 0.4f), 180f, 180f, true, Offset(x - s * 0.22f, c.y - s * 0.75f), Size(s * 0.44f, s * 1.3f))
            }
            drawCircle(Color(0xFFA8A29E), s * 0.25f, c + Offset(flip * s * 1.05f, -s * 0.05f))
            drawLine(Color(0xFFA8A29E), c + Offset(-flip * s * 0.9f, -s * 0.05f), c + Offset(-flip * s * 1.4f, s * 0.2f), s * 0.1f, StrokeCap.Round)
            for (dx in listOf(-0.5f, 0.5f)) drawLine(dark, c + Offset(dx * s, -s * 0.05f), c + Offset(dx * s + sin(t * 8) * s * 0.1f, s * 0.35f), s * 0.1f, StrokeCap.Round)
        }
        Body.HEDGEHOG -> {   // round body bristling with crystal quills
            for (k in 0 until 18) { val a = PI.toFloat() * (k / 17f) + PI.toFloat()
                drawLine(glow, c + Offset(cos(a) * s * 0.6f, sin(a) * s * 0.5f), c + Offset(cos(a) * s * (1.15f + 0.08f * sin(t * 6 + k)), sin(a) * s * 1.0f), s * 0.05f, StrokeCap.Round) }
            drawOval(Brush.radialGradient(listOf(Color(0xFF78716C), body), c, s), Offset(c.x - s * 0.85f, c.y - s * 0.55f), Size(s * 1.7f, s * 1.0f))
            drawCircle(Color(0xFFD6D3D1), s * 0.3f, c + Offset(flip * s * 0.8f, s * 0.1f))
            drawCircle(Color.Black, s * 0.07f, c + Offset(flip * s * 1.05f, s * 0.12f))
            drawCircle(Color.Black, s * 0.05f, c + Offset(flip * s * 0.85f, 0f))
        }
        Body.GORILLA -> {   // hulking ape, knuckle-walking, mushrooms on the back
            val breathe = sin(t * 2) * s * 0.05f
            drawOval(Brush.radialGradient(listOf(Color(0xFF3F3F46), body), c, s * 1.2f), Offset(c.x - s * 0.9f, c.y - s * 0.7f - breathe), Size(s * 1.8f, s * 1.3f))
            for (side in listOf(-1f, 1f)) drawLine(Color(0xFF27272A), c + Offset(side * s * 0.7f, -s * 0.3f), c + Offset(side * s * 1.1f, s * 0.9f), s * 0.32f, StrokeCap.Round)
            drawCircle(Color(0xFF27272A), s * 0.4f, c + Offset(0f, -s * 0.85f - breathe))
            drawOval(Color(0xFF57534E), Offset(c.x - s * 0.25f, c.y - s * 0.8f - breathe), Size(s * 0.5f, s * 0.3f))
            eye(d, c + Offset(-s * 0.13f, -s * 0.95f - breathe), s * 0.06f, Color(0xFFEF4444)); eye(d, c + Offset(s * 0.13f, -s * 0.95f - breathe), s * 0.06f, Color(0xFFEF4444))
            for (k in 0 until 3) { val m = c + Offset((k - 1) * s * 0.45f, -s * 0.75f)
                drawLine(Color(0xFFE7E5E4), m, m + Offset(0f, -s * 0.3f), s * 0.06f); drawArc(glow, 180f, 180f, true, m + Offset(-s * 0.2f, -s * 0.5f), Size(s * 0.4f, s * 0.35f)) }
        }
        Body.TURTLE -> {   // domed rock shell with floating crystal shards above it
            drawOval(Color(0xFF65A30D).copy(alpha = 0.9f), Offset(c.x + flip * s * 0.6f, c.y - s * 0.05f), Size(s * 0.6f, s * 0.4f))
            for (dx in listOf(-0.6f, 0.5f)) drawOval(Color(0xFF4D7C0F), Offset(c.x + dx * s - s * 0.15f, c.y + s * 0.2f), Size(s * 0.3f, s * 0.4f))
            drawArc(Brush.radialGradient(listOf(Color(0xFF57534E), body), c, s), 180f, 180f, true, Offset(c.x - s * 0.85f, c.y - s * 0.7f), Size(s * 1.7f, s * 1.4f))
            for (k in 0 until 5) drawCircle(Color(0xFF292524), s * 0.13f, c + Offset((k - 2) * s * 0.3f, -s * 0.35f + abs(k - 2) * s * 0.07f), style = Stroke(s * 0.03f))
            for (k in 0 until 4) { val p = c + Offset((k - 1.5f) * s * 0.45f, -s * (1.05f + 0.15f * sin(t * 2 + k)))
                drawRect(glow.copy(alpha = 0.8f), p - Offset(s * 0.08f, s * 0.08f), Size(s * 0.16f, s * 0.16f)) }
        }
        Body.ELEPHANT -> {   // massive: big ears, swinging trunk, tusks
            for (dx in listOf(-0.55f, -0.2f, 0.25f, 0.6f)) drawRect(dark, Offset(c.x + dx * s - s * 0.12f, c.y + s * 0.2f), Size(s * 0.24f, s * 0.75f))
            drawOval(Brush.radialGradient(listOf(Color(0xFF52525B), body), c, s * 1.2f), Offset(c.x - s * 0.95f, c.y - s * 0.6f), Size(s * 1.9f, s * 1.0f))
            val hd = c + Offset(flip * s * 0.95f, -s * 0.35f)
            drawOval(Color(0xFF3F3F46), Offset(hd.x - s * 0.35f, hd.y - s * 0.45f), Size(s * 0.6f, s * 0.8f))
            drawCircle(body, s * 0.35f, hd)
            val trunk = Path().apply { moveTo(hd.x + flip * s * 0.2f, hd.y + s * 0.1f); quadraticBezierTo(hd.x + flip * s * (0.6f + 0.1f * sin(t * 2)), hd.y + s * 0.5f, hd.x + flip * s * (0.4f + 0.15f * sin(t * 2)), hd.y + s * 0.95f) }
            drawPath(trunk, body, style = Stroke(s * 0.16f, cap = StrokeCap.Round))
            drawLine(Color(0xFFF5F5F4), hd + Offset(flip * s * 0.15f, s * 0.2f), hd + Offset(flip * s * 0.5f, s * 0.35f), s * 0.07f, StrokeCap.Round)
            crystals(c + Offset(-flip * s * 0.1f, -s * 0.5f), s * 0.9f, glow, 4, s * 1.1f, t)
        }
        Body.RHINO -> {   // armoured charger with a big crystal horn
            val charge = sin(t * 7) * s * 0.06f
            for (dx in listOf(-0.6f, -0.3f, 0.3f, 0.55f)) drawLine(dark, c + Offset(dx * s, s * 0.2f), c + Offset(dx * s + charge, s * 0.85f), s * 0.18f, StrokeCap.Round)
            drawRoundRect(Brush.verticalGradient(listOf(Color(0xFF71717A), body), c.y - s * 0.6f, c.y + s * 0.4f), Offset(c.x - s * 0.95f, c.y - s * 0.55f), Size(s * 1.8f, s * 0.9f), androidx.compose.ui.geometry.CornerRadius(s * 0.4f))
            val hd = c + Offset(flip * s * 0.95f, -s * 0.05f)
            drawOval(body, Offset(hd.x - s * 0.35f, hd.y - s * 0.25f), Size(s * 0.75f, s * 0.5f))
            val horn = Path().apply { moveTo(hd.x + flip * s * 0.25f, hd.y - s * 0.15f); lineTo(hd.x + flip * s * 0.55f, hd.y - s * 0.75f); lineTo(hd.x + flip * s * 0.45f, hd.y - s * 0.1f); close() }
            drawPath(horn, glow)
            drawCircle(Color(0xFFEF4444), s * 0.05f, hd + Offset(flip * s * 0.05f, -s * 0.1f))
        }
        Body.EEL -> {   // long ribbon eel with a dorsal fin, undulating
            val pts = (0..20).map { i -> val f = i / 20f; Offset(c.x + flip * (f - 0.5f) * s * 3f, c.y + sin(t * 4 - f * 9) * s * 0.35f) }
            for (i in 0 until 20) drawLine(body, pts[i], pts[i + 1], s * (0.3f - i * 0.011f), StrokeCap.Round)
            for (i in 2 until 18 step 2) drawLine(glow.copy(alpha = 0.6f), pts[i], pts[i] + Offset(0f, -s * 0.25f), s * 0.04f)
            eye(d, pts[20] + Offset(-flip * s * 0.1f, -s * 0.06f), s * 0.07f, Color(0xFFF0ABFC))
            drawLine(Color.Black, pts[20], pts[20] + Offset(-flip * s * 0.18f, s * 0.08f), s * 0.03f)
        }
        Body.LIZARD -> {   // crested lizard, tail curling, tongue flicking
            val tail = Path().apply { moveTo(c.x - flip * s * 0.6f, c.y); cubicTo(c.x - flip * s * 1.4f, c.y + s * 0.2f, c.x - flip * s * 1.6f, c.y - s * 0.6f, c.x - flip * s * (1.2f + 0.1f * sin(t * 3)), c.y - s * 0.8f) }
            drawPath(tail, body, style = Stroke(s * 0.15f, cap = StrokeCap.Round))
            for ((dx, ph) in listOf(-0.4f to 0f, 0.35f to 1.5f)) for (side in listOf(-1f, 1f))
                drawLine(dark, c + Offset(dx * s, 0f), c + Offset(dx * s + sin(t * 6 + ph) * s * 0.15f, side * s * 0.45f), s * 0.07f, StrokeCap.Round)
            drawOval(Brush.horizontalGradient(listOf(body, glow)), Offset(c.x - s * 0.7f, c.y - s * 0.22f), Size(s * 1.4f, s * 0.44f))
            for (k in 0 until 6) drawLine(glow, c + Offset((k - 2.5f) * s * 0.2f, -s * 0.18f), c + Offset((k - 2.5f) * s * 0.2f, -s * 0.4f), s * 0.04f)
            val hd = c + Offset(flip * s * 0.85f, 0f)
            drawOval(body, Offset(hd.x - s * 0.25f, hd.y - s * 0.18f), Size(s * 0.5f, s * 0.36f))
            if (sin(t * 5) > 0.6f) drawLine(Color(0xFFF43F5E), hd + Offset(flip * s * 0.25f, 0f), hd + Offset(flip * s * 0.55f, 0f), s * 0.03f)
            eye(d, hd + Offset(flip * s * 0.05f, -s * 0.06f), s * 0.05f)
        }
        Body.SHARK -> {   // torpedo body, dorsal fin, tail beat
            val beat = sin(t * 5) * s * 0.15f
            val tail = Path().apply { moveTo(c.x - flip * s * 1.0f, c.y); lineTo(c.x - flip * s * 1.5f, c.y - s * 0.45f + beat); lineTo(c.x - flip * s * 1.35f, c.y + beat * 0.2f); lineTo(c.x - flip * s * 1.5f, c.y + s * 0.35f + beat); close() }
            drawPath(tail, body)
            drawOval(Brush.verticalGradient(listOf(body, Color(0xFFCBD5E1)), c.y - s * 0.4f, c.y + s * 0.4f), Offset(c.x - s * 1.1f, c.y - s * 0.38f), Size(s * 2.3f, s * 0.76f))
            val fin = Path().apply { moveTo(c.x - flip * s * 0.1f, c.y - s * 0.35f); lineTo(c.x - flip * s * 0.35f, c.y - s * 0.9f); lineTo(c.x + flip * s * 0.3f, c.y - s * 0.35f); close() }
            drawPath(fin, glow)
            for (k in 0 until 3) drawLine(dark, c + Offset(flip * (s * 0.4f + k * s * 0.1f), -s * 0.1f), c + Offset(flip * (s * 0.35f + k * s * 0.1f), s * 0.15f), s * 0.03f)
            eye(d, c + Offset(flip * s * 0.8f, -s * 0.08f), s * 0.05f, Color.White)
            drawArc(Color.White, 0f, 180f, false, Offset(c.x + flip * s * 0.7f - s * 0.2f, c.y), Size(s * 0.4f, s * 0.2f), style = Stroke(s * 0.04f))
        }
        Body.OCTOPUS -> {   // bulbous head (brain-like), eight curling arms
            for (i in 0 until 8) {
                val a0 = PI.toFloat() * (0.1f + i / 9f)
                val path = Path().apply { var p = c + Offset(0f, s * 0.2f); moveTo(p.x, p.y)
                    for (k in 1..6) { val a = a0 + sin(t * 2 + i + k * 0.6f) * 0.4f; p = p + Offset(cos(a) * s * 0.22f, sin(a) * s * 0.22f); lineTo(p.x, p.y) } }
                drawPath(path, body, style = Stroke(s * 0.11f, cap = StrokeCap.Round))
            }
            drawOval(Brush.radialGradient(listOf(Color(0xFFF9A8D4), body), c + Offset(0f, -s * 0.4f), s), Offset(c.x - s * 0.7f, c.y - s * 1.2f), Size(s * 1.4f, s * 1.4f))
            for (k in 0 until 4) drawArc(Color(0xFF9D174D).copy(alpha = 0.6f), 200f, 140f, false, Offset(c.x - s * (0.5f - k * 0.1f), c.y - s * (1.05f - k * 0.15f)), Size(s * (1f - k * 0.2f), s * 0.4f), style = Stroke(s * 0.03f))
            eye(d, c + Offset(-s * 0.25f, -s * 0.2f), s * 0.1f); eye(d, c + Offset(s * 0.25f, -s * 0.2f), s * 0.1f)
        }
        Body.ANGLER -> {   // deep-sea anglerfish with a glowing lure and fangs
            val lure = c + Offset(flip * s * (1.0f + 0.1f * sin(t * 3)), -s * 1.2f)
            drawLine(body, c + Offset(flip * s * 0.2f, -s * 0.55f), lure, s * 0.04f)
            drawCircle(Brush.radialGradient(listOf(Color.White, glow, Color.Transparent), lure, s * 0.4f), s * 0.4f, lure)
            drawCircle(Brush.radialGradient(listOf(Color(0xFF1E293B), body), c, s), s * 0.75f, c)
            val tail = Path().apply { moveTo(c.x - flip * s * 0.7f, c.y); lineTo(c.x - flip * s * 1.3f, c.y - s * 0.4f); lineTo(c.x - flip * s * 1.3f, c.y + s * 0.4f); close() }
            drawPath(tail, body)
            val mouth = c + Offset(flip * s * 0.45f, s * 0.2f)
            drawArc(Color.Black, if (flip > 0) -60f else 120f, 120f, true, Offset(mouth.x - s * 0.35f, mouth.y - s * 0.35f), Size(s * 0.7f, s * 0.7f))
            for (k in 0 until 5) drawLine(Color.White, mouth + Offset(flip * k * s * 0.06f, -s * 0.2f + k * s * 0.08f), mouth + Offset(flip * (k * s * 0.06f + s * 0.06f), -s * 0.1f + k * s * 0.08f), s * 0.03f)
            eye(d, c + Offset(flip * s * 0.25f, -s * 0.3f), s * 0.08f, glow)
        }
        Body.HORNET -> {   // striped abdomen with a stinger, buzzing wings
            val flap = 0.4f + 0.6f * abs(sin(t * 25))
            for (side in listOf(-1f, 1f)) drawOval(Color.White.copy(alpha = 0.35f), Offset(c.x - s * 0.15f - s * 0.45f, c.y - s * 0.3f - s * 0.9f * flap * (if (side > 0) 1f else 0.7f)), Size(s * 0.9f, s * 0.9f * flap))
            val ab = c + Offset(-flip * s * 0.6f, s * 0.1f)
            drawOval(Color(0xFFFACC15), Offset(ab.x - s * 0.55f, ab.y - s * 0.32f), Size(s * 1.1f, s * 0.64f))
            for (k in 0 until 3) drawRect(Color.Black, Offset(ab.x - s * 0.35f + k * s * 0.3f, ab.y - s * 0.32f), Size(s * 0.12f, s * 0.64f))
            drawLine(dark, ab + Offset(-flip * s * 0.55f, 0f), ab + Offset(-flip * s * 0.85f, s * 0.05f), s * 0.05f, StrokeCap.Round)
            drawCircle(Color(0xFF422006), s * 0.3f, c + Offset(flip * s * 0.1f, 0f))
            drawCircle(Color(0xFF422006), s * 0.25f, c + Offset(flip * s * 0.55f, -s * 0.05f))
            eye(d, c + Offset(flip * s * 0.68f, -s * 0.12f), s * 0.09f, glow)
            for (k in 0 until 3) drawLine(dark, c + Offset(flip * s * (0.0f + k * 0.15f), s * 0.2f), c + Offset(flip * s * (-0.1f + k * 0.2f), s * 0.6f), s * 0.035f)
        }
        Body.CATERPILLAR -> {   // fat round segments with prolegs and antennae, inching
            val inch = sin(t * 4)
            for (i in 0 until 7) {
                val x = c.x + flip * (i - 3) * s * 0.32f
                val y = c.y - (if (i in 2..4) inch.coerceAtLeast(0f) * s * 0.35f * (1 - abs(i - 3) / 2f) else 0f)
                drawLine(dark, Offset(x, y + s * 0.2f), Offset(x, y + s * 0.42f), s * 0.07f, StrokeCap.Round)
                drawCircle(Brush.radialGradient(listOf(glow, body), Offset(x, y), s * 0.32f), s * 0.28f, Offset(x, y))
                drawCircle(Color(0xFFFDE047).copy(alpha = 0.8f), s * 0.05f, Offset(x, y - s * 0.12f))
            }
            val hd = c + Offset(flip * s * 1.2f, -s * 0.05f)
            drawCircle(body, s * 0.3f, hd)
            for (side in listOf(-1f, 1f)) drawLine(dark, hd + Offset(side * s * 0.1f, -s * 0.25f), hd + Offset(side * s * 0.25f + flip * s * 0.1f, -s * 0.55f), s * 0.03f)
            eye(d, hd + Offset(flip * s * 0.12f, -s * 0.05f), s * 0.06f)
        }
        Body.ANT -> {   // three-part body, elbowed antennae, mandibles, six legs
            for (i in 0 until 3) for (side in listOf(-1f, 1f)) {
                val base = c + Offset((i - 1) * s * 0.25f, 0f)
                drawLine(dark, base, base + Offset((i - 1) * s * 0.4f + sin(t * 10 + i + side) * s * 0.1f, side * s * 0.6f), s * 0.05f, StrokeCap.Round)
            }
            drawOval(Color(0xFFE7E5E4), Offset(c.x - flip * s * 0.6f - s * 0.45f, c.y - s * 0.32f), Size(s * 0.9f, s * 0.64f))
            drawOval(body, Offset(c.x - s * 0.2f, c.y - s * 0.17f), Size(s * 0.4f, s * 0.34f))
            val hd = c + Offset(flip * s * 0.5f, -s * 0.05f)
            drawCircle(Color(0xFFD6D3D1), s * 0.25f, hd)
            for (side in listOf(-1f, 1f)) {
                drawLine(dark, hd + Offset(0f, -s * 0.15f), hd + Offset(flip * s * 0.15f, -s * 0.5f), s * 0.03f)
                drawLine(dark, hd + Offset(flip * s * 0.15f, -s * 0.5f), hd + Offset(flip * s * (0.5f + side * 0.05f), -s * 0.6f), s * 0.03f)
            }
            drawArc(glow, -40f, 80f, false, Offset(hd.x + flip * s * 0.05f - s * 0.25f, hd.y - s * 0.1f), Size(s * 0.5f, s * 0.35f), style = Stroke(s * 0.05f))
        }
        Body.MUSHROOM -> {   // living mushroom cluster with spotted caps and acid drips
            for ((dx, h, r) in listOf(Triple(-0.6f, 0.7f, 0.45f), Triple(0.55f, 0.55f, 0.38f), Triple(0f, 1.1f, 0.65f))) {
                val top = c + Offset(dx * s + sin(t * 2 + dx) * s * 0.05f, s * 0.8f - h * s)
                drawLine(Color(0xFFE7E5E4), Offset(dx * s + c.x, c.y + s * 0.8f), top, s * 0.18f * r / 0.5f, StrokeCap.Round)
                drawArc(Brush.verticalGradient(listOf(glow, body), top.y - r * s, top.y), 180f, 180f, true, Offset(top.x - r * s, top.y - r * s * 0.8f), Size(r * s * 2, r * s * 1.6f))
                for (k in 0 until 3) drawCircle(Color.White.copy(alpha = 0.7f), r * s * 0.12f, top + Offset((k - 1) * r * s * 0.5f, -r * s * 0.45f))
                val drip = (t * 0.8f + dx) % 1f
                drawCircle(Color(0xFFA3E635).copy(alpha = 1f - drip), s * 0.05f, top + Offset(r * s * 0.6f, drip * s * 0.6f))
            }
            eye(d, c + Offset(-s * 0.1f, s * 0.35f), s * 0.06f, Color(0xFFA3E635)); eye(d, c + Offset(s * 0.1f, s * 0.35f), s * 0.06f, Color(0xFFA3E635))
        }
        Body.ANEMONE -> {   // sea anemone: column base and a crown of swaying tentacles
            drawRoundRect(Brush.verticalGradient(listOf(body, dark), c.y, c.y + s), Offset(c.x - s * 0.45f, c.y), Size(s * 0.9f, s * 0.9f), androidx.compose.ui.geometry.CornerRadius(s * 0.3f))
            for (k in 0 until 14) {
                val a = -PI.toFloat() * (0.05f + k / 14.5f)
                val sway = sin(t * 2 + k * 0.5f) * 0.25f
                val tip = c + Offset(cos(a + sway) * s * 1.1f, sin(a + sway) * s * 1.0f)
                drawLine(glow.copy(alpha = 0.85f), c, tip, s * 0.08f, StrokeCap.Round)
                drawCircle(Color.White.copy(alpha = 0.8f), s * 0.06f, tip)
            }
            drawCircle(Color(0xFF831843), s * 0.15f, c)
        }
        Body.HIVE -> {   // hanging crystal hive with a buzzing swarm around it
            drawLine(Color(0xFF78350F), c + Offset(0f, -s * 1.6f), c + Offset(0f, -s * 1.0f), s * 0.06f)
            for (k in 0 until 4) { val w = s * (1.2f - abs(k - 1.5f) * 0.25f)
                drawOval(Brush.horizontalGradient(listOf(body, glow, body)), Offset(c.x - w / 2, c.y - s * 1.0f + k * s * 0.42f), Size(w, s * 0.48f)) }
            drawOval(Color.Black, Offset(c.x - s * 0.15f, c.y + s * 0.15f), Size(s * 0.3f, s * 0.25f))
            for (k in 0 until 9) { val a = t * (2f + k % 3) + k * 0.7f
                val p = c + Offset(cos(a) * s * (1.2f + 0.2f * (k % 2)), sin(a * 1.3f) * s * 0.9f)
                drawOval(Color(0xFFFACC15), Offset(p.x - s * 0.08f, p.y - s * 0.05f), Size(s * 0.16f, s * 0.1f))
                drawLine(Color.White.copy(alpha = 0.5f), p, p + Offset(0f, -s * 0.08f), s * 0.05f) }
        }
        Body.EGG -> {   // crystal egg on spindly legs, cracking, something peeks out
            for (side in listOf(-1f, 1f)) for (i in 0 until 2) drawLine(dark, c + Offset(side * s * 0.3f, s * 0.4f), c + Offset(side * s * (0.6f + i * 0.25f), s * 0.95f), s * 0.05f, StrokeCap.Round)
            drawOval(Brush.radialGradient(listOf(Color(0xFFFDE68A), glow, body), c + Offset(-s * 0.2f, -s * 0.3f), s * 1.2f), Offset(c.x - s * 0.6f, c.y - s * 1.0f), Size(s * 1.2f, s * 1.5f))
            val crack = Path().apply { moveTo(c.x - s * 0.5f, c.y - s * 0.35f); lineTo(c.x - s * 0.25f, c.y - s * 0.2f); lineTo(c.x, c.y - s * 0.45f); lineTo(c.x + s * 0.25f, c.y - s * 0.2f); lineTo(c.x + s * 0.5f, c.y - s * 0.4f) }
            drawPath(crack, Color(0xFFFFF7ED), style = Stroke(s * 0.04f))
            if (sin(t * 1.5f) > 0.3f) { eye(d, c + Offset(-s * 0.12f, -s * 0.28f), s * 0.05f, Color(0xFFF43F5E)); eye(d, c + Offset(s * 0.12f, -s * 0.28f), s * 0.05f, Color(0xFFF43F5E)) }
        }
        Body.DRONE -> {   // quad-rotor drone with a scanning eye
            val hover = sin(t * 3) * s * 0.1f
            val cc = c + Offset(0f, hover)
            for ((dx, dy) in listOf(-1f to -1f, 1f to -1f, -1f to 1f, 1f to 1f)) {
                val r = cc + Offset(dx * s * 0.85f, dy * s * 0.45f)
                drawLine(body, cc, r, s * 0.08f)
                drawOval(Color.White.copy(alpha = 0.3f), Offset(r.x - s * 0.4f * abs(cos(t * 30)), r.y - s * 0.05f), Size(s * 0.8f * abs(cos(t * 30)) + 2f, s * 0.1f))
                drawCircle(dark, s * 0.08f, r)
            }
            drawCircle(Brush.radialGradient(listOf(Color(0xFF475569), body), cc, s * 0.5f), s * 0.45f, cc)
            drawCircle(Color.Black, s * 0.25f, cc)
            drawCircle(glow, s * 0.14f, cc + Offset(sin(t * 2) * s * 0.07f, 0f))
            val beam = Path().apply { moveTo(cc.x, cc.y + s * 0.3f); lineTo(cc.x - s * 0.5f, cc.y + s * 1.4f); lineTo(cc.x + s * 0.5f, cc.y + s * 1.4f); close() }
            drawPath(beam, glow.copy(alpha = 0.15f + 0.1f * sin(t * 5)))
        }
        Body.STARFISH -> {   // five-armed star, slowly rotating, with a glowing core
            rotate(t * 25f, c) {
                val star = Path().apply {
                    for (k in 0 until 10) { val a = -PI.toFloat() / 2 + k * PI.toFloat() / 5; val r = if (k % 2 == 0) s * 1.2f else s * 0.45f
                        val p = c + Offset(cos(a) * r, sin(a) * r); if (k == 0) moveTo(p.x, p.y) else lineTo(p.x, p.y) }
                    close()
                }
                drawPath(star, Brush.radialGradient(listOf(Color.White, glow, body), c, s * 1.2f))
                for (k in 0 until 5) { val a = -PI.toFloat() / 2 + k * 2 * PI.toFloat() / 5
                    for (j in 1..3) drawCircle(Color.White.copy(alpha = 0.6f), s * 0.04f, c + Offset(cos(a) * s * 0.28f * j, sin(a) * s * 0.28f * j)) }
            }
            drawCircle(glow, s * (0.15f + 0.05f * sin(t * 6)), c)
            drawCircle(glow.copy(alpha = 0.6f), s * 1.45f, c, style = Stroke(s * 0.03f))
        }
        Body.FIREFLY -> {   // small beetle with a pulsing lantern abdomen
            val bob = sin(t * 3) * s * 0.2f
            val cc = c + Offset(0f, bob)
            val glowA = 0.5f + 0.5f * sin(t * 4)
            drawCircle(Brush.radialGradient(listOf(Color(0xFFFEF08A).copy(alpha = glowA), Color.Transparent), cc + Offset(-flip * s * 0.4f, s * 0.1f), s * 1.2f), s * 1.2f, cc + Offset(-flip * s * 0.4f, s * 0.1f))
            for (side in listOf(-1f, 1f)) drawOval(Color.White.copy(alpha = 0.35f), Offset(cc.x - s * 0.5f, cc.y - s * 0.55f - side * s * 0.1f * sin(t * 20)), Size(s * 0.8f, s * 0.35f))
            drawOval(Color(0xFFFDE047).copy(alpha = 0.5f + glowA * 0.5f), Offset(cc.x - flip * s * 0.7f - s * 0.25f, cc.y - s * 0.15f), Size(s * 0.6f, s * 0.4f))
            drawOval(body, Offset(cc.x - s * 0.3f, cc.y - s * 0.2f), Size(s * 0.6f, s * 0.4f))
            drawCircle(Color(0xFFDC2626), s * 0.15f, cc + Offset(flip * s * 0.35f, -s * 0.05f))
            for (side in listOf(-1f, 1f)) drawLine(dark, cc + Offset(flip * s * 0.45f, -s * 0.1f), cc + Offset(flip * s * 0.75f, -s * (0.3f + side * 0.1f)), s * 0.025f)
        }
        Body.BEETLE -> {   // stag beetle with huge mandibles and a hard shell
            for (i in 0 until 3) for (side in listOf(-1f, 1f)) drawLine(dark, c + Offset((i - 1) * s * 0.35f, 0f), c + Offset((i - 1) * s * 0.55f + sin(t * 8 + i) * s * 0.08f, side * s * 0.75f), s * 0.06f, StrokeCap.Round)
            drawOval(Brush.verticalGradient(listOf(glow, body, dark), c.y - s * 0.5f, c.y + s * 0.5f), Offset(c.x - flip * s * 0.2f - s * 0.65f, c.y - s * 0.5f), Size(s * 1.3f, s * 1.0f))
            drawLine(Color.White.copy(alpha = 0.25f), c + Offset(-flip * s * 0.2f, -s * 0.45f), c + Offset(-flip * s * 0.2f, s * 0.45f), s * 0.03f)
            val hd = c + Offset(flip * s * 0.65f, 0f)
            drawCircle(dark, s * 0.25f, hd)
            for (side in listOf(-1f, 1f)) {
                val open = 0.25f + 0.15f * sin(t * 3)
                val jaw = Path().apply { moveTo(hd.x, hd.y + side * s * 0.1f); quadraticBezierTo(hd.x + flip * s * 0.6f, hd.y + side * s * (0.6f + open), hd.x + flip * s * 0.95f, hd.y + side * s * 0.05f) }
                drawPath(jaw, glow, style = Stroke(s * 0.08f, cap = StrokeCap.Round))
            }
        }
        Body.PHOENIX -> {   // fiery bird with sweeping wings and a flame tail
            val flap = sin(t * 5)
            for (side in listOf(-1f, 1f)) {
                val wing = Path().apply { moveTo(c.x, c.y); quadraticBezierTo(c.x + side * s * 1.0f, c.y - s * (1.2f * flap + 0.2f), c.x + side * s * 1.9f, c.y - s * 0.6f * flap); lineTo(c.x + side * s * 0.6f, c.y + s * 0.2f); close() }
                drawPath(wing, Brush.radialGradient(listOf(Color(0xFFFDE047), glow, body), c, s * 2f))
            }
            for (k in 0 until 4) { val tail = Path().apply { moveTo(c.x, c.y + s * 0.2f); quadraticBezierTo(c.x + (k - 1.5f) * s * 0.3f, c.y + s * 0.9f, c.x + (k - 1.5f) * s * 0.5f + sin(t * 4 + k) * s * 0.15f, c.y + s * 1.5f) }
                drawPath(tail, Color(0xFFF97316).copy(alpha = 0.8f), style = Stroke(s * 0.07f, cap = StrokeCap.Round)) }
            drawOval(Color(0xFFFB923C), Offset(c.x - s * 0.2f, c.y - s * 0.4f), Size(s * 0.4f, s * 0.7f))
            drawCircle(Color(0xFFFDBA74), s * 0.2f, c + Offset(0f, -s * 0.55f))
            drawLine(Color(0xFFFDE047), c + Offset(0f, -s * 0.55f), c + Offset(flip * s * 0.3f, -s * 0.5f), s * 0.06f)
            eye(d, c + Offset(flip * s * 0.06f, -s * 0.6f), s * 0.04f, Color.White)
        }
        Body.DEER -> {   // ghostly stag: slender legs, crystal antlers, walking
            for ((dx, ph) in listOf(-0.55f to 0f, -0.35f to 1.5f, 0.4f to 0.7f, 0.6f to 2.2f))
                drawLine(body, c + Offset(dx * s, s * 0.15f), c + Offset(dx * s + sin(t * 5 + ph) * s * 0.12f, s * 1.0f), s * 0.07f, StrokeCap.Round)
            drawOval(Brush.horizontalGradient(listOf(body.copy(alpha = 0.7f), Color(0xFFE2E8F0))), Offset(c.x - s * 0.75f, c.y - s * 0.3f), Size(s * 1.5f, s * 0.55f))
            val neck = c + Offset(flip * s * 0.85f, -s * 0.7f)
            drawLine(Color(0xFFE2E8F0), c + Offset(flip * s * 0.55f, -s * 0.1f), neck, s * 0.18f, StrokeCap.Round)
            drawOval(Color(0xFFE2E8F0), Offset(neck.x - s * 0.15f + flip * s * 0.1f, neck.y - s * 0.1f), Size(s * 0.35f, s * 0.2f))
            for (side in listOf(-1f, 1f)) {
                val base = neck + Offset(side * s * 0.05f, -s * 0.1f)
                val tip = base + Offset(side * s * 0.4f, -s * 0.6f)
                drawLine(glow, base, tip, s * 0.04f); drawLine(glow, base + (tip - base) * 0.5f, base + (tip - base) * 0.5f + Offset(side * s * 0.25f, -s * 0.05f), s * 0.03f)
                drawLine(glow, base + (tip - base) * 0.75f, base + (tip - base) * 0.75f + Offset(-side * s * 0.05f, -s * 0.25f), s * 0.03f)
            }
            drawCircle(Color(0xFF38BDF8), s * 0.04f, neck + Offset(flip * s * 0.1f, -s * 0.02f))
        }
        Body.EAGLE -> {   // bird of prey soaring with spread wing feathers
            val glide = sin(t * 2) * s * 0.15f
            for (side in listOf(-1f, 1f)) {
                val wing = Path().apply { moveTo(c.x, c.y); lineTo(c.x + side * s * 0.9f, c.y - s * 0.35f - glide); lineTo(c.x + side * s * 1.9f, c.y - s * 0.1f - glide * 1.5f); lineTo(c.x + side * s * 0.8f, c.y + s * 0.15f); close() }
                drawPath(wing, Brush.horizontalGradient(listOf(body, glow.copy(alpha = 0.8f))))
                for (k in 0 until 4) drawLine(dark, c + Offset(side * s * (1.4f + k * 0.12f), -s * 0.15f - glide * 1.3f), c + Offset(side * s * (1.5f + k * 0.12f), s * 0.05f - glide), s * 0.04f)
            }
            drawOval(body, Offset(c.x - s * 0.2f, c.y - s * 0.3f), Size(s * 0.4f, s * 0.8f))
            drawCircle(Color(0xFFF5F5F4), s * 0.2f, c + Offset(0f, -s * 0.4f))
            val beak = Path().apply { moveTo(c.x + flip * s * 0.12f, c.y - s * 0.45f); lineTo(c.x + flip * s * 0.35f, c.y - s * 0.38f); lineTo(c.x + flip * s * 0.12f, c.y - s * 0.32f); close() }
            drawPath(beak, Color(0xFFFBBF24))
            eye(d, c + Offset(flip * s * 0.07f, -s * 0.45f), s * 0.035f)
            val tail = Path().apply { moveTo(c.x - s * 0.15f, c.y + s * 0.4f); lineTo(c.x, c.y + s * 0.85f); lineTo(c.x + s * 0.15f, c.y + s * 0.4f); close() }
            drawPath(tail, body)
        }
        Body.MANTA -> {   // manta ray gliding with rippling wing edges
            val ripple = sin(t * 3)
            val manta = Path().apply {
                moveTo(c.x, c.y - s * 0.5f)
                quadraticBezierTo(c.x + s * 1.2f, c.y - s * (0.5f + 0.3f * ripple), c.x + s * 1.9f, c.y + s * 0.1f * ripple)
                quadraticBezierTo(c.x + s * 0.8f, c.y + s * 0.3f, c.x, c.y + s * 0.5f)
                quadraticBezierTo(c.x - s * 0.8f, c.y + s * 0.3f, c.x - s * 1.9f, c.y + s * 0.1f * ripple)
                quadraticBezierTo(c.x - s * 1.2f, c.y - s * (0.5f + 0.3f * ripple), c.x, c.y - s * 0.5f)
            }
            drawPath(manta, Brush.verticalGradient(listOf(body, glow.copy(alpha = 0.6f)), c.y - s * 0.6f, c.y + s * 0.5f))
            drawLine(body, c + Offset(0f, s * 0.45f), c + Offset(sin(t * 3) * s * 0.2f, s * 1.4f), s * 0.04f, StrokeCap.Round)
            for (side in listOf(-1f, 1f)) drawLine(body, c + Offset(side * s * 0.2f, -s * 0.45f), c + Offset(side * s * 0.3f, -s * 0.75f), s * 0.08f, StrokeCap.Round)
            for (side in listOf(-1f, 1f)) drawCircle(glow, s * 0.06f, c + Offset(side * s * 0.25f, -s * 0.3f))
        }
        Body.MOSQUITO -> {   // long legs, needle proboscis, thin wings
            val hov = sin(t * 4) * s * 0.1f
            val cc = c + Offset(0f, hov)
            for (i in 0 until 3) for (side in listOf(-1f, 1f)) {
                val k = cc + Offset((i - 1) * s * 0.2f + side * s * 0.4f, -s * 0.1f)
                drawLine(dark, cc, k, s * 0.025f); drawLine(dark, k, k + Offset(side * s * 0.35f, s * 1.0f), s * 0.025f)
            }
            for (side in listOf(-1f, 1f)) drawOval(Color.White.copy(alpha = 0.3f), Offset(cc.x - s * 0.7f, cc.y - s * 0.45f - side * s * 0.05f * sin(t * 30)), Size(s * 1.0f, s * 0.3f))
            drawOval(Brush.horizontalGradient(listOf(glow, body)), Offset(cc.x - flip * s * 0.4f - s * 0.45f, cc.y - s * 0.12f), Size(s * 0.9f, s * 0.24f))
            drawCircle(body, s * 0.17f, cc + Offset(flip * s * 0.25f, -s * 0.05f))
            drawLine(Color(0xFFE879F9), cc + Offset(flip * s * 0.38f, -s * 0.02f), cc + Offset(flip * s * 1.1f, s * 0.25f), s * 0.03f)
            eye(d, cc + Offset(flip * s * 0.3f, -s * 0.1f), s * 0.06f, Color(0xFFF43F5E))
        }
        Body.CROCODILE -> {   // long jaws snapping, scaly back, short legs
            val snap = 0.15f + 0.25f * abs(sin(t * 2.5f))
            for (dx in listOf(-0.6f, 0.4f)) for (side in listOf(-1f, 1f)) drawLine(dark, c + Offset(dx * s, 0f), c + Offset(dx * s + side * s * 0.15f, s * 0.45f), s * 0.12f, StrokeCap.Round)
            drawOval(Brush.verticalGradient(listOf(body, Color(0xFF14532D)), c.y - s * 0.3f, c.y + s * 0.3f), Offset(c.x - s * 1.0f, c.y - s * 0.3f), Size(s * 1.9f, s * 0.6f))
            drawLine(body, c + Offset(-flip * s * 0.9f, 0f), c + Offset(-flip * s * 1.7f, s * (0.1f + 0.1f * sin(t * 2))), s * 0.2f, StrokeCap.Round)
            for (k in 0 until 7) drawCircle(glow, s * 0.06f, c + Offset((k - 3) * s * 0.25f, -s * 0.28f))
            val hd = c + Offset(flip * s * 0.85f, -s * 0.05f)
            rotate(-flip * snap * 45f, hd) { drawRect(body, Offset(if (flip > 0) hd.x else hd.x - s * 0.9f, hd.y - s * 0.18f), Size(s * 0.9f, s * 0.16f)) }
            drawRect(body, Offset(if (flip > 0) hd.x else hd.x - s * 0.9f, hd.y), Size(s * 0.9f, s * 0.16f))
            for (k in 0 until 5) drawLine(Color.White, hd + Offset(flip * s * (0.15f + k * 0.15f), 0f), hd + Offset(flip * s * (0.2f + k * 0.15f), -s * 0.07f), s * 0.03f)
            eye(d, hd + Offset(flip * s * 0.05f, -s * 0.2f), s * 0.07f)
        }
        else -> drawCircle(body, s * 0.6f, c)
    }
}
