package com.operator.companion.relay

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.media.ExifInterface
import android.media.MediaMetadataRetriever
import android.media.MediaScannerConnection
import android.os.Build
import android.os.Environment
import android.os.StatFs
import android.webkit.MimeTypeMap
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.RandomAccessFile
import java.nio.file.Files
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

class FsException(message: String, val status: Int = 400) : Exception(message)

class FsResult(val data: JSONObject? = null, val bytes: ByteArray? = null)

/**
 * Storage operations requested by the laptop over the relay socket.
 * Mirrors the server's ADB backend (same JSON shapes and the same safety rules).
 */
class RemoteFileManager(private val context: Context) {

    private class Upload(
        val part: File, val dir: File, val dirPath: String, val name: String,
        val overwrite: Boolean, val raf: RandomAccessFile
    ) {
        @Volatile var lastWrite: Long = System.currentTimeMillis()
    }

    private val uploads = ConcurrentHashMap<String, Upload>()

    companion object {
        private const val SCAN_PAGE = 5000
        private val SHARED_ROOTS = listOf("/sdcard", "/mnt/sdcard", "/storage")
        private val VOLUME_PARENTS = setOf("/", "/storage", "/storage/emulated", "/storage/self", "/mnt")
        private val VOLUME_ROOTS = setOf("/sdcard", "/mnt/sdcard", "/storage/emulated/0", "/storage/self/primary")
        private val SHORTCUTS = listOf(
            "Camera" to "DCIM/Camera", "Screenshots" to "Pictures/Screenshots", "Screenshots" to "DCIM/Screenshots",
            "Downloads" to "Download", "Pictures" to "Pictures", "Movies" to "Movies", "Music" to "Music",
            "Documents" to "Documents", "WhatsApp" to "Android/media/com.whatsapp/WhatsApp/Media",
            "WhatsApp" to "WhatsApp/Media"
        )
        private const val MAX_READ = 1024 * 1024

        fun hasStorageAccess(context: Context): Boolean =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) Environment.isExternalStorageManager()
            else listOf(Manifest.permission.READ_EXTERNAL_STORAGE, Manifest.permission.WRITE_EXTERNAL_STORAGE).all {
                context.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED
            }
    }

    fun handle(op: String, args: JSONObject, payload: ByteArray?): FsResult {
        if (op != "roots") requireAccess()
        return when (op) {
            "roots" -> FsResult(roots())
            "list" -> FsResult(list(args.getString("path")))
            "stat" -> args.getString("path").let { p -> FsResult(entry(existing(p), normalize(p))) }
            "read" -> FsResult(bytes = read(args.getString("path"), args.getLong("offset"), args.getInt("length")))
            "thumb" -> FsResult(bytes = thumbnail(args.getString("path"), args.optInt("size", 256)))
            "walk" -> FsResult(walk(args.getJSONArray("paths")))
            "scan" -> FsResult(bytes = scan(args.getString("path"), args.optInt("page", 0), args.optString("scan_id")))
            "mkdir" -> FsResult(mkdir(args.getString("path")))
            "delete" -> FsResult(delete(args.getJSONArray("paths")))
            "rename" -> FsResult(rename(args.getString("path"), args.getString("new_name")))
            "transfer" -> FsResult(transfer(args.getJSONArray("paths"), args.getString("dest_dir"), args.optBoolean("copy")))
            "write_open" -> FsResult(writeOpen(args.getString("dir"), args.getString("rel"), args.optBoolean("overwrite")))
            "write" -> FsResult(write(args.getString("handle"), args.getLong("offset"), payload ?: ByteArray(0)))
            "write_close" -> FsResult(writeClose(args.getString("handle"), args.optLong("size", -1)))
            "write_abort" -> FsResult(writeAbort(args.getString("handle")))
            else -> throw FsException("Unknown storage operation: $op")
        }
    }

    // ---------- Phone Backup scan ----------
    private class ScanCache(val id: String, val at: Long, val entries: List<Triple<String, Long, Long>>)
    private val scanCache = ConcurrentHashMap<String, ScanCache>()

    /** Whole-tree listing for Phone Backup, done here in one walk instead of one round trip per folder.
     *  Returns gzip JSON {"entries": [[rel, size, mtimeSec]], "next": page|null, "scan_id"}, 5,000 entries per page.
     *  Page 0 walks afresh; later pages must name page 0's scan_id and come from that same walk (never a
     *  re-walk: a shifted list would make files look deleted), else 410 and the laptop skips this folder. */
    private fun scan(path: String, page: Int, scanId: String): ByteArray {
        val p = normalize(path)
        val now = System.currentTimeMillis()
        scanCache.entries.removeIf { now - it.value.at > 600_000 }
        val walk = if (page == 0) {
            val tree = walkTree(p)  // age counts from the end of the walk, which can take minutes
            ScanCache(UUID.randomUUID().toString(), System.currentTimeMillis(), tree).also { scanCache[p] = it }
        } else {
            scanCache[p]?.takeIf { it.id == scanId } ?: throw FsException("Folder scan expired — sync again", 410)
        }
        val entries = walk.entries
        val from = page.coerceAtLeast(0) * SCAN_PAGE
        val arr = JSONArray()
        for (i in from until minOf(entries.size, from + SCAN_PAGE)) {
            val (rel, size, mtime) = entries[i]
            arr.put(JSONArray().put(rel).put(size).put(mtime))
        }
        val more = from + SCAN_PAGE < entries.size
        if (!more) scanCache.remove(p, walk)  // last page served: free the memory
        val next: Any = if (more) page + 1 else JSONObject.NULL
        val out = ByteArrayOutputStream()
        java.util.zip.GZIPOutputStream(out).use {
            it.write(JSONObject().put("entries", arr).put("next", next).put("scan_id", walk.id).toString().toByteArray())
        }
        return out.toByteArray()
    }

    /** Any unreadable subfolder fails the whole scan: a partial list would make its files look deleted. */
    private fun walkTree(root: String): List<Triple<String, Long, Long>> {
        val top = File(root)
        if (!top.exists()) throw FsException("$root does not exist", 404)
        if (!top.isDirectory) throw FsException("$root is not a folder", 409)
        val out = ArrayList<Triple<String, Long, Long>>()
        val stack = ArrayDeque<Pair<File, String>>().apply { addLast(top to "") }
        while (stack.isNotEmpty()) {
            val (dir, rel) = stack.removeLast()
            val children = dir.listFiles() ?: throw FsException("Android does not allow access to ${dir.path}", 403)
            for (c in children) {
                val r = if (rel.isEmpty()) c.name else "$rel/${c.name}"
                if (c.isDirectory) {
                    if (c.name != ".thumbnails" && !Files.isSymbolicLink(c.toPath())) stack.addLast(c to r)
                } else if (!c.name.startsWith(".trashed-") && !c.name.startsWith(".pending-")) {
                    out.add(Triple(r, c.length(), c.lastModified() / 1000))
                }
            }
        }
        return out
    }

    // ---------- paths & guards ----------
    private fun requireAccess() {
        if (!hasStorageAccess(context)) throw FsException(
            "Storage access is not granted on the phone: open Operator Companion and allow 'All files access'", 403)
    }

    private fun normalize(path: String): String {
        if (!path.startsWith("/") || path.contains('\u0000') || path.contains('\n')) throw FsException("Invalid path: $path")
        val parts = ArrayDeque<String>()
        for (seg in path.split("/")) when (seg) {
            "", "." -> {}
            ".." -> parts.removeLastOrNull()
            else -> parts.addLast(seg)
        }
        return "/" + parts.joinToString("/")
    }

    private fun inShared(p: String) = SHARED_ROOTS.any { p == it || p.startsWith("$it/") }

    private fun writableDir(path: String): String {
        val p = normalize(path)
        if (!inShared(p) || p in setOf("/storage", "/storage/emulated", "/storage/self"))
            throw FsException("Writing is only allowed inside shared storage, not $p", 403)
        return p
    }

    private fun removable(path: String): String {
        val p = normalize(path)
        if (!inShared(p) || p in VOLUME_ROOTS || (p.substringBeforeLast('/').ifEmpty { "/" }) in VOLUME_PARENTS)
            throw FsException("Refusing to modify protected location $p", 403)
        return p
    }

    private fun validName(name: String): String {
        val n = name.trim()
        if (n.isEmpty() || n == "." || n == ".." || n.contains('/') || n.contains('\u0000') || n.contains('\n'))
            throw FsException("Invalid name: $name")
        return n
    }

    private fun existing(path: String): File {
        val f = File(normalize(path))
        if (!f.exists()) throw FsException("$path does not exist", 404)
        return f
    }

    private fun mimeOf(name: String): String? =
        MimeTypeMap.getSingleton().getMimeTypeFromExtension(name.substringAfterLast('.', "").lowercase())

    private fun entry(f: File, path: String): JSONObject {
        val dir = f.isDirectory
        return JSONObject()
            .put("name", f.name.ifEmpty { "/" })
            .put("path", path)
            .put("is_dir", dir)
            .put("is_link", runCatching { Files.isSymbolicLink(f.toPath()) }.getOrDefault(false))
            .put("size", if (dir) 0 else f.length())
            .put("mtime", f.lastModified() / 1000)
            .put("mode", (if (dir) "d" else "-") + (if (f.canRead()) "r" else "-") + (if (f.canWrite()) "w" else "-"))
            .put("hidden", f.name.startsWith("."))
            .put("mime", if (dir) JSONObject.NULL else (mimeOf(f.name) ?: JSONObject.NULL))
    }

    private fun scan(paths: List<String>) {
        runCatching { MediaScannerConnection.scanFile(context, paths.take(200).toTypedArray(), null, null) }
    }

    // ---------- read ----------
    private fun roots(): JSONObject {
        val volumes = JSONArray().put(JSONObject().put("label", "Internal storage").put("path", "/sdcard"))
        context.getExternalFilesDirs(null).drop(1).filterNotNull().forEach { d ->
            val root = d.absolutePath.substringBefore("/Android/data/")
            volumes.put(JSONObject().put("label", "External (${root.substringAfterLast('/')})").put("path", root))
        }
        val shortcuts = JSONArray()
        val seen = mutableSetOf<String>()
        for ((label, rel) in SHORTCUTS) {
            if (label !in seen && File("/sdcard/$rel").isDirectory) {
                seen += label
                shortcuts.put(JSONObject().put("label", label).put("path", "/sdcard/$rel"))
            }
        }
        val space = runCatching {
            val s = StatFs(Environment.getExternalStorageDirectory().path)
            JSONObject().put("total", s.totalBytes).put("free", s.availableBytes)
        }.getOrNull()
        return JSONObject().put("volumes", volumes).put("shortcuts", shortcuts)
            .put("internal_space", space ?: JSONObject.NULL).put("sdk", Build.VERSION.SDK_INT)
            .put("storage_access", hasStorageAccess(context))
    }

    private fun list(path: String): JSONObject {
        val p = normalize(path)
        val f = File(p)
        if (!f.exists()) throw FsException("$p does not exist", 404)
        if (!f.isDirectory) throw FsException("$p is not a folder", 409)
        val children = f.listFiles() ?: throw FsException("Android does not allow access to $p", 403)
        val items = JSONArray()
        children.forEach { c -> items.put(entry(c, if (p == "/") "/${c.name}" else "$p/${c.name}")) }
        return JSONObject().put("path", p).put("parent", if (p == "/") JSONObject.NULL else p.substringBeforeLast('/').ifEmpty { "/" })
            .put("items", items)
    }

    private fun read(path: String, offset: Long, length: Int): ByteArray {
        val f = existing(path)
        if (f.isDirectory) throw FsException("$path is a folder", 409)
        RandomAccessFile(f, "r").use { raf ->
            val n = minOf(length.coerceIn(0, MAX_READ).toLong(), (raf.length() - offset).coerceAtLeast(0)).toInt()
            val buf = ByteArray(n)
            raf.seek(offset)
            var got = 0
            while (got < n) {
                val r = raf.read(buf, got, n - got)
                if (r < 0) break
                got += r
            }
            return if (got == n) buf else buf.copyOf(got)
        }
    }

    private fun walk(paths: JSONArray): JSONObject {
        val files = JSONArray()
        val dirs = JSONArray()
        var count = 0
        fun visit(f: File, rel: String) {
            if (++count > 50_000) throw FsException("Too many files to bundle (over 50,000)", 413)
            if (f.isDirectory) {
                dirs.put(rel)
                f.listFiles()?.forEach { visit(it, "$rel/${it.name}") }
            } else if (f.canRead()) {
                files.put(JSONObject().put("path", f.path).put("rel", rel).put("size", f.length()))
            }
        }
        for (i in 0 until paths.length()) existing(paths.getString(i)).let { visit(it, it.name) }
        return JSONObject().put("files", files).put("dirs", dirs)
    }

    private fun thumbnail(path: String, size: Int): ByteArray {
        val f = existing(path)
        val mime = mimeOf(f.name) ?: ""
        val bmp = when {
            mime.startsWith("image/") -> decodeImage(f, size)
            mime.startsWith("video/") -> videoFrame(f, size)
            else -> null
        } ?: throw FsException("No preview available", 415)
        val out = ByteArrayOutputStream()
        bmp.compress(Bitmap.CompressFormat.JPEG, 75, out)
        bmp.recycle()
        return out.toByteArray()
    }

    private fun fit(b: Bitmap, size: Int): Bitmap {
        val scale = size.toFloat() / maxOf(b.width, b.height)
        if (scale >= 1f) return b
        val s = Bitmap.createScaledBitmap(b, (b.width * scale).toInt().coerceAtLeast(1), (b.height * scale).toInt().coerceAtLeast(1), true)
        if (s !== b) b.recycle()
        return s
    }

    private fun decodeImage(f: File, size: Int): Bitmap? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(f.path, bounds)
        if (bounds.outWidth <= 0) return null
        var sample = 1
        while (bounds.outWidth / (sample * 2) >= size && bounds.outHeight / (sample * 2) >= size) sample *= 2
        val raw = BitmapFactory.decodeFile(f.path, BitmapFactory.Options().apply { inSampleSize = sample }) ?: return null
        val b = fit(raw, size)
        val degrees = runCatching {
            when (ExifInterface(f.path).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)) {
                ExifInterface.ORIENTATION_ROTATE_90 -> 90f
                ExifInterface.ORIENTATION_ROTATE_180 -> 180f
                ExifInterface.ORIENTATION_ROTATE_270 -> 270f
                else -> 0f
            }
        }.getOrDefault(0f)
        if (degrees == 0f) return b
        val r = Bitmap.createBitmap(b, 0, 0, b.width, b.height, Matrix().apply { postRotate(degrees) }, true)
        if (r !== b) b.recycle()
        return r
    }

    private fun videoFrame(f: File, size: Int): Bitmap? {
        val r = MediaMetadataRetriever()
        return try {
            r.setDataSource(f.path)
            val frame = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1)
                r.getScaledFrameAtTime(1_000_000, MediaMetadataRetriever.OPTION_CLOSEST_SYNC, size, size)
            else r.getFrameAtTime(1_000_000, MediaMetadataRetriever.OPTION_CLOSEST_SYNC)
            (frame ?: r.frameAtTime)?.let { fit(it, size) }
        } catch (e: Exception) {
            null
        } finally {
            runCatching { r.release() }
        }
    }

    // ---------- write ----------
    private fun mkdir(path: String): JSONObject {
        val p = normalize(path)
        writableDir(p.substringBeforeLast('/').ifEmpty { "/" })
        val f = File(p)
        if (f.exists()) throw FsException("Already exists", 409)
        if (!f.mkdirs()) throw FsException("Could not create $p", 500)
        return JSONObject().put("path", p)
    }

    private fun delete(paths: JSONArray): JSONObject {
        val targets = (0 until paths.length()).map { removable(paths.getString(it)) }
        val failed = targets.filter { val f = File(it); f.exists() && !f.deleteRecursively() }
        scan(targets)
        if (failed.isNotEmpty()) throw FsException("Could not delete: ${failed.joinToString()}", 500)
        return JSONObject().put("deleted", JSONArray(targets))
    }

    private fun rename(path: String, newName: String): JSONObject {
        val src = File(removable(path))
        if (!src.exists()) throw FsException("$path does not exist", 404)
        val dstPath = normalize(path).substringBeforeLast('/') + "/" + validName(newName)
        val dst = File(dstPath)
        if (dstPath == src.path) return JSONObject().put("path", dstPath)
        if (dstPath.equals(src.path, ignoreCase = true)) { // case-only rename on case-insensitive storage
            val tmp = File(src.path + ".acc_rename_" + System.currentTimeMillis())
            if (!src.renameTo(tmp) || !tmp.renameTo(dst)) throw FsException("Rename failed", 500)
        } else {
            if (dst.exists()) throw FsException("A file with that name already exists", 409)
            if (!src.renameTo(dst)) throw FsException("Rename failed", 500)
        }
        scan(listOf(src.path, dstPath))
        return JSONObject().put("path", dstPath)
    }

    private fun transfer(paths: JSONArray, destDir: String, copy: Boolean): JSONObject {
        val dest = File(writableDir(destDir))
        if (!dest.isDirectory) throw FsException("$destDir is not a folder", 409)
        val realDest = dest.canonicalPath.lowercase()
        val results = JSONArray()
        for (i in 0 until paths.length()) {
            val srcPath = if (copy) normalize(paths.getString(i)) else removable(paths.getString(i))
            val src = File(srcPath)
            val dstPath = "${dest.path}/${src.name}"
            val dst = File(dstPath)
            val res = JSONObject().put("path", srcPath).put("dest", dstPath)
            try {
                val realSrc = src.canonicalPath.lowercase()
                if (!src.exists()) throw FsException("Source no longer exists")
                if (realDest == realSrc || realDest.startsWith("$realSrc/")) throw FsException("Cannot place a folder inside itself")
                if (dst.exists()) throw FsException("Already exists in destination")
                val ok = if (copy) src.copyRecursively(dst, overwrite = false)
                else src.renameTo(dst) || (src.copyRecursively(dst, overwrite = false) && src.deleteRecursively())
                if (!ok) throw FsException(if (copy) "Copy failed" else "Move failed")
                res.put("ok", true)
            } catch (e: Exception) {
                res.put("ok", false).put("error", e.message ?: e.javaClass.simpleName)
            }
            results.put(res)
        }
        scan((0 until results.length()).map { results.getJSONObject(it) }.filter { it.optBoolean("ok") }.map { it.getString("dest") })
        return JSONObject().put("results", results)
    }

    private fun writeOpen(dir: String, rel: String, overwrite: Boolean): JSONObject {
        val now = System.currentTimeMillis()
        uploads.entries.removeIf { (_, u) -> (now - u.lastWrite > 10 * 60_000L).also { idle -> if (idle) abort(u) } }
        val parts = rel.split("/").filter { it.isNotEmpty() }.map { validName(it) }
        if (parts.isEmpty()) throw FsException("Missing file name")
        val dirPath = (listOf(writableDir(dir).trimEnd('/')) + parts.dropLast(1)).joinToString("/")
        val target = File(dirPath)
        if (!target.isDirectory && !target.mkdirs()) throw FsException("Could not create folder $dirPath", 500)
        // Leftovers from uploads interrupted before this process started
        target.listFiles { f -> f.name.startsWith(".acc-upload-") && f.name.endsWith(".part") && now - f.lastModified() > 3600_000L }
            ?.forEach { it.delete() }
        val handle = UUID.randomUUID().toString().replace("-", "").take(16)
        val part = File(target, ".acc-upload-$handle.part")
        uploads[handle] = Upload(part, target, dirPath, parts.last(), overwrite, RandomAccessFile(part, "rw"))
        return JSONObject().put("handle", handle)
    }

    private fun upload(handle: String) = uploads[handle] ?: throw FsException("Upload expired or unknown", 410)

    private fun write(handle: String, offset: Long, data: ByteArray): JSONObject {
        val u = upload(handle)
        synchronized(u.raf) {
            u.raf.seek(offset)
            u.raf.write(data)
        }
        u.lastWrite = System.currentTimeMillis()
        return JSONObject().put("written", data.size)
    }

    private fun writeClose(handle: String, size: Long): JSONObject {
        val u = upload(handle)
        uploads.remove(handle)
        synchronized(u.raf) { u.raf.close() }
        if (size >= 0 && u.part.length() != size) {
            u.part.delete()
            throw FsException("Upload incomplete (${u.part.length()} of $size bytes)", 500)
        }
        var name = u.name
        var final = File(u.dir, name)
        if (u.overwrite) {
            if (final.isFile) final.delete()
        } else {
            val stem = name.substringBeforeLast('.', name)
            val ext = if (name.contains('.') && !name.startsWith(".")) "." + name.substringAfterLast('.') else ""
            var i = 1
            while (final.exists()) {
                name = if (ext.isEmpty()) "${u.name} ($i)" else "$stem ($i)$ext"
                final = File(u.dir, name)
                i++
            }
        }
        if (!u.part.renameTo(final)) {
            u.part.delete()
            throw FsException("Could not save ${u.name}", 500)
        }
        val path = "${u.dirPath}/$name"
        scan(listOf(final.path))
        return JSONObject().put("path", path)
    }

    private fun abort(u: Upload) {
        runCatching { synchronized(u.raf) { u.raf.close() } }
        u.part.delete()
    }

    /** Connection lost: the laptop can no longer finish or abort these uploads. */
    fun abortAll() {
        uploads.keys.toList().forEach { h -> uploads.remove(h)?.let { abort(it) } }
    }

    private fun writeAbort(handle: String): JSONObject {
        uploads.remove(handle)?.let { abort(it) }
        return JSONObject()
    }
}
