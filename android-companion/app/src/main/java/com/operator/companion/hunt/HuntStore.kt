package com.operator.companion.hunt

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import org.json.JSONObject

/** Crystal Mines HUNT progress (which DNA samples are collected) + live instructions from the laptop. */
object HuntStore {
    private const val PREFS = "hunt"
    val collected = MutableStateFlow<Set<String>>(emptySet())
    val active = MutableStateFlow(false)
    val shards = MutableStateFlow(0)
    val xp = MutableStateFlow(0)
    val muted = MutableStateFlow(false)

    fun level(x: Int = xp.value) = 1 + x / 250
    fun levelProgress(x: Int = xp.value) = (x % 250) / 250f

    /** Rewards per capture by rarity: crystal shards + XP. */
    fun reward(r: Rarity) = when (r) {
        Rarity.COMMON -> 5 to 40; Rarity.UNCOMMON -> 10 to 70; Rarity.RARE -> 20 to 120
        Rarity.EPIC -> 40 to 200; Rarity.LEGENDARY -> 100 to 400
    }

    /** The 9 original signatures the Crystal Overlord demands. */
    val ORIGINAL_NINE = listOf("crystalclaw", "shardfang", "deepcrawler", "glowwing", "rockmaw", "venomshard", "nightglider", "toxicore", "earthreaper")

    /** Species that may appear next: rarer tiers unlock as the collection grows; commons are most frequent. */
    fun nextSpecies(exclude: String? = null, scene: SceneSense.Kind = SceneSense.Kind.UNKNOWN): Species? {
        val have = collected.value
        val tierOpen = mapOf(Rarity.COMMON to 0, Rarity.UNCOMMON to 3, Rarity.RARE to 8, Rarity.EPIC to 16, Rarity.LEGENDARY to 25)
        val pool = SPECIES.filter { it.id !in have && it.id != exclude }.filter {
            if (it.challenge == Challenge.ALL) ORIGINAL_NINE.all { id -> id in have } else have.size >= (tierOpen[it.rarity] ?: 0)
        }
        if (pool.isEmpty()) return null
        // the original ten come first-ish: give them extra weight
        val weights = pool.map { sp -> (when (sp.rarity) { Rarity.COMMON -> 10; Rarity.UNCOMMON -> 6; Rarity.RARE -> 4; Rarity.EPIC -> 2; Rarity.LEGENDARY -> 1 }) *
            (if (sp.id in ORIGINAL_NINE || sp.challenge == Challenge.ALL) 3 else 1) *
            // outdoors: flyers + plant dwellers are more likely; indoors: crawlers and ground mammals
            (when (scene) {
                SceneSense.Kind.OUTDOOR -> if (sp.habitat() == Habitat.SKY || sp.habitat() == Habitat.PLANT) 2 else 1
                SceneSense.Kind.INDOOR -> if (sp.habitat() == Habitat.GROUND) 2 else 1
                else -> 1
            }) }
        var r = kotlin.random.Random.nextInt(weights.sum())
        pool.forEachIndexed { i, sp -> r -= weights[i]; if (r < 0) return sp }
        return pool.last()
    }

    /** Latest instruction from the laptop: text + optional arrow (left/right/up/down/forward/scan) + id to re-show. */
    data class Command(val text: String, val arrow: String?, val id: Long)
    val command = MutableStateFlow<Command?>(null)

    /** Dashboard steering of the current creature: left/right/up/down/closer/farther/summon/freeze. */
    data class Steer(val dir: String, val id: Long)
    val steer = MutableStateFlow<Steer?>(null)

    fun init(ctx: Context) {
        val p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val valid = SPECIES.map { it.id }.toSet()   // drop ids of species that no longer exist
        collected.value = p.getStringSet("dna", emptySet())!!.filter { it in valid }.toSet()
        shards.value = p.getInt("shards", 0); xp.value = p.getInt("xp", 0); muted.value = p.getBoolean("muted", false)
    }

    private fun save(ctx: Context) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putStringSet("dna", collected.value)
            .putInt("shards", shards.value).putInt("xp", xp.value).putBoolean("muted", muted.value).apply()
    }

    /** Returns (shards, xp) gained. */
    fun collect(ctx: Context, id: String): Pair<Int, Int> {
        val sp = speciesById(id)
        val (s, x) = if (sp != null) reward(sp.rarity) else 0 to 0
        collected.value = collected.value + id
        shards.value += s; xp.value += x
        save(ctx)
        return s to x
    }

    fun addShards(ctx: Context, n: Int) { shards.value = (shards.value + n).coerceAtLeast(0); save(ctx) }
    fun spend(ctx: Context, n: Int): Boolean { if (shards.value < n) return false; shards.value -= n; save(ctx); return true }
    fun addXp(ctx: Context, n: Int) { xp.value += n; save(ctx) }
    fun toggleMute(ctx: Context) { muted.value = !muted.value; save(ctx); if (muted.value) HuntSound.stopAll() }

    fun reset(ctx: Context) {
        collected.value = emptySet(); shards.value = 0; xp.value = 0
        save(ctx)
    }

    /** HUNT_COMMAND from the dashboard (only shown while a hunt is open). */
    fun onCommand(p: JSONObject) {
        p.optString("steer").takeIf { it in setOf("left", "right", "up", "down", "closer", "farther", "summon", "freeze") }?.let {
            steer.value = Steer(it, System.currentTimeMillis()); return
        }
        val text = p.optString("text").take(140)
        val arrow = p.optString("arrow").takeIf { it in setOf("left", "right", "up", "down", "forward", "back", "scan") }
        if (text.isBlank() && arrow == null) return
        command.value = Command(text, arrow, System.currentTimeMillis())
    }
}
