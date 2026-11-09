package com.leonp.assistant

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Optional, user-enabled accessibility bridge for Rusty. Rusty does not publish a
 * MediaSession and its public API omits the track title, so this reads the title
 * it already renders and presses its own transport buttons. Nothing leaves the
 * tablet except the compact device context sent to the user's relay.
 */
class NowPlayingService : AccessibilityService() {
    companion object {
        @Volatile
        private var instance: NowPlayingService? = null

        @Volatile
        private var latestTitle: String? = null

        @Volatile
        private var latestArtist: String? = null

        @Volatile
        private var latestAt = 0L

        private val placeholders = setOf(
            "Track title",
            "Artist",
            "Album",
            "Waiting for Spotify",
            "Ready for Spotify",
            "Select this receiver from Spotify",
            "Now playing album art",
        )

        fun isEnabled(context: Context): Boolean {
            val manager = context.getSystemService(android.view.accessibility.AccessibilityManager::class.java)
            return manager.getEnabledAccessibilityServiceList(AccessibilityServiceInfo.FEEDBACK_ALL_MASK)
                .any {
                    val info = it.resolveInfo?.serviceInfo ?: return@any false
                    info.packageName == context.packageName &&
                        info.name == NowPlayingService::class.java.name
                }
        }

        fun nowPlaying(): Pair<String, String>? {
            val title = latestTitle?.trim().orEmpty()
            if (title.isEmpty() || title in placeholders) return null
            if (System.currentTimeMillis() - latestAt > 120_000) return null
            val artist = latestArtist?.trim().orEmpty().takeUnless { it in placeholders }.orEmpty()
            return title to artist
        }

        fun performMediaAction(action: String, timeoutMs: Long): Boolean {
            val service = instance ?: return false
            val deadline = SystemClock.uptimeMillis() + timeoutMs
            var attempts = 0
            do {
                attempts++
                if (service.click(action, "attempt_$attempts")) return true
                try {
                    Thread.sleep(80)
                } catch (_: InterruptedException) {
                    return false
                }
            } while (SystemClock.uptimeMillis() < deadline)
            return false
        }
    }

    private fun click(action: String, attempt: String): Boolean {
        val latch = CountDownLatch(1)
        val clicked = AtomicBoolean(false)
        handler.post {
            try {
                for (window in windows) {
                    val root = window.root ?: continue
                    if (root.packageName?.toString() != "dev.rusty.app") continue
                    val node = when (action) {
                        "next" -> findButton(root, "btnNext", "Next track")
                        "previous" -> findButton(root, "btnPrev", "Previous track")
                        else -> findButton(root, "btnPlayPause", "Play or pause")
                    } ?: continue
                    if (!node.isEnabled) continue
                    clicked.set(node.performAction(AccessibilityNodeInfo.ACTION_CLICK))
                    if (clicked.get()) break
                }
            } catch (_: Exception) {
                // Stale window; the caller retries.
            } finally {
                latch.countDown()
            }
        }
        latch.await(500, TimeUnit.MILLISECONDS)
        return clicked.get()
    }

    private val handler = Handler(Looper.getMainLooper())

    private val readRunnable = Runnable { readNow() }

    override fun onServiceConnected() {
        instance = this
        handler.postDelayed(readRunnable, 300)
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event?.packageName?.toString() != "dev.rusty.app") return
        if (event.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED &&
            event.eventType != AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED
        ) {
            return
        }
        handler.removeCallbacks(readRunnable)
        handler.postDelayed(readRunnable, 250)
    }

    override fun onInterrupt() = Unit

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        if (instance === this) instance = null
        super.onDestroy()
    }

    private fun readNow() {
        try {
            for (window in windows) {
                val root = window.root ?: continue
                if (root.packageName?.toString() != "dev.rusty.app") continue
                val title = findText(
                    root,
                    setOf("tvFullTitle", "ssWatchTitle", "ssDetailTitle", "dlnaTitle"),
                ) ?: continue
                val artist = findText(
                    root,
                    setOf("tvFullArtist", "ssWatchArtist", "ssDetailArtist", "dlnaArtist"),
                ).orEmpty()
                val cleanTitle = title.trim()
                if (cleanTitle.isEmpty() || cleanTitle in placeholders) continue
                latestTitle = cleanTitle
                latestArtist = artist.trim().takeUnless { it in placeholders }.orEmpty()
                latestAt = System.currentTimeMillis()
                return
            }
        } catch (_: Exception) {
            // A window can disappear between getWindows() and getChild().
        }
    }

    private fun findText(node: AccessibilityNodeInfo, ids: Set<String>): String? {
        val id = node.viewIdResourceName?.substringAfterLast('/')
        if (id != null && id in ids && node.isVisibleToUser) {
            val text = node.text?.toString()?.trim()
            if (!text.isNullOrEmpty()) return text
        }
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            val found = findText(child, ids)
            if (found != null) return found
        }
        return null
    }

    private fun click(action: String): Boolean {
        val latch = CountDownLatch(1)
        val clicked = AtomicBoolean(false)
        handler.post {
            try {
                for (window in windows) {
                    val root = window.root ?: continue
                    if (root.packageName?.toString() != "dev.rusty.app") continue
                    val node = when (action) {
                        "next" -> findButton(root, "btnNext", "Next track")
                        "previous" -> findButton(root, "btnPrev", "Previous track")
                        else -> findButton(root, "btnPlayPause", "Play or pause")
                    } ?: continue
                    if (!node.isEnabled) continue
                    clicked.set(node.performAction(AccessibilityNodeInfo.ACTION_CLICK))
                    if (clicked.get()) break
                }
            } catch (_: Exception) {
                // Stale window; the caller retries.
            } finally {
                latch.countDown()
            }
        }
        latch.await(800, TimeUnit.MILLISECONDS)
        return clicked.get()
    }

    private fun findButton(
        root: AccessibilityNodeInfo,
        id: String,
        description: String,
    ): AccessibilityNodeInfo? {
        val byId = try {
            root.findAccessibilityNodeInfosByViewId("dev.rusty.app:id/$id")
        } catch (_: Exception) {
            emptyList()
        }
        byId.firstOrNull { it.isVisibleToUser }?.let { return it }
        return findByDescription(root, description)
    }

    private fun findByDescription(
        node: AccessibilityNodeInfo,
        description: String,
    ): AccessibilityNodeInfo? {
        if (node.isVisibleToUser && node.contentDescription?.toString() == description) return node
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            val found = findByDescription(child, description)
            if (found != null) return found
        }
        return null
    }
}
