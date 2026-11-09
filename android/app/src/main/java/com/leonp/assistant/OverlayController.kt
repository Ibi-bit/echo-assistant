package com.leonp.assistant

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.os.SystemClock
import android.text.SpannableString
import android.text.TextUtils
import android.text.style.ForegroundColorSpan
import android.text.style.StyleSpan
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.TextView
import kotlin.math.abs
import kotlin.math.min
import kotlin.math.sin

/**
 * Small state icon for the pill. While listening it draws EQ bars driven by the
 * live mic level; while speaking the bars stand steady; thinking pulses dots.
 */
class StatusIconView(context: Context) : View(context) {
    private var mode = "idle"
    private var level = 0f
    private var lastTick = 0L

    private val accent = Paint().apply { color = 0xFF7DD3FC.toInt() }
    private val dim = Paint().apply { color = 0xFF334155.toInt() }
    private val errorPaint = Paint().apply { color = 0xFFF87171.toInt() }
    private val dotPaint = Paint().apply { color = 0xFF7DD3FC.toInt() }

    private val animator = object : Runnable {
        override fun run() {
            invalidate()
            if (mode == "listening" || mode == "thinking" || mode == "speaking") {
                postDelayed(this, 90)
            }
        }
    }

    fun setMode(next: String) {
        if (mode == next) return
        mode = next
        if (next != "listening") level = 0f
        removeCallbacks(animator)
        if (next == "listening" || next == "thinking" || next == "speaking") {
            postDelayed(animator, 90)
        }
        invalidate()
    }

    /** Smoothed mic level 0..1, fed at ~12 Hz while listening. */
    fun setLevel(value: Float) {
        level = 0.55f * level + 0.45f * value.coerceIn(0f, 1f)
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat()
        val h = height.toFloat()
        val now = SystemClock.elapsedRealtime()
        when (mode) {
            "listening" -> {
                // EQ bars that follow the real mic level, with a gentle floor.
                val base = maxOf(level, 0.16f + 0.10f * abs(sin(now / 520f)))
                for (i in 0 until 5) {
                    val wave = 0.35f + 0.65f * abs(sin(now / 260f + i * 1.1f))
                    bar(canvas, i, 5, h * 0.15f + h * 0.85f * min(1f, base * wave * 1.6f), accent)
                }
            }
            "speaking" -> {
                for (i in 0 until 5) {
                    val shimmer = 0.8f + 0.2f * abs(sin(now / 300f + i * 0.9f))
                    bar(canvas, i, 5, h * 0.15f + h * 0.85f * 0.75f * shimmer, accent)
                }
            }
            "thinking" -> {
                for (i in 0 until 3) {
                    val x = w * (0.18f + i * 0.32f)
                    val pulse = 0.3f + 0.7f * maxOf(0f, sin(now / 200f - i * 1.2f))
                    dotPaint.alpha = (pulse * 255).toInt()
                    canvas.drawCircle(x, h / 2f, w * 0.11f, dotPaint)
                }
                dotPaint.alpha = 255
            }
            "error" -> {
                for (i in 0 until 5) bar(canvas, i, 5, h * 0.3f, errorPaint)
            }
            else -> {
                // idle / offline: quiet dim bars
                for (i in 0 until 3) bar(canvas, i, 3, h * 0.25f, dim)
            }
        }
    }

    private fun bar(canvas: Canvas, i: Int, count: Int, heightPx: Float, paint: Paint) {
        val w = width.toFloat()
        val h = height.toFloat()
        val slot = w / count
        val barW = slot * 0.55f
        val x = slot * i + (slot - barW) / 2f
        val top = (h - heightPx) / 2f
        canvas.drawRoundRect(x, top, x + barW, top + heightPx, barW / 2f, barW / 2f, paint)
    }
}

/**
 * The assistant's only on-screen presence: a small, non-focusable card over Rusty.
 * Touches outside the card go straight through to Rusty.
 */
class OverlayController(
    context: Context,
    private val onTap: () -> Unit,
    private val onLongPress: () -> Unit,
) {
    private val appContext = context.applicationContext
    private val windowManager =
        appContext.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private val main = android.os.Handler(android.os.Looper.getMainLooper())

    private var root: LinearLayout? = null
    private var icon: StatusIconView? = null
    private var status: TextView? = null
    private var detail: TextView? = null
    private var hideDetail: Runnable? = null

    private var speechStartMs = 0L
    private var speechDurationSec = 0f
    private var lastBoldSpan: Pair<Int, Int>? = null

    private val speechTick = object : Runnable {
        override fun run() {
            highlightCurrentWord()
            main.postDelayed(this, 100)
        }
    }

    fun show() = main.post {
        if (root != null) return@post

        val container = LinearLayout(appContext).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            minimumWidth = dp(180)
            setPadding(dp(22), dp(11), dp(22), dp(11))
            background = android.graphics.drawable.GradientDrawable().apply {
                cornerRadius = dp(24).toFloat()
                setColor(0xE60B0F14.toInt())
                setStroke(dp(1), 0x667DD3FC)
            }
            elevation = dp(10).toFloat()
            isClickable = true
            isFocusable = false
            setOnClickListener { onTap() }
            setOnLongClickListener {
                onLongPress()
                true
            }
        }

        val header = LinearLayout(appContext).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        val iconView = StatusIconView(appContext).apply {
            layoutParams = LinearLayout.LayoutParams(dp(20), dp(20)).apply {
                marginEnd = dp(9)
            }
        }
        val label = TextView(appContext).apply {
            text = "Hey Jarvis"
            setTextColor(0xFF7DD3FC.toInt())
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 17f)
            typeface = Typeface.DEFAULT_BOLD
            maxWidth = dp(640)
            gravity = Gravity.CENTER
        }
        header.addView(iconView)
        header.addView(label)

        val body = TextView(appContext).apply {
            setTextColor(0xFFE2E8F0.toInt())
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
            maxWidth = dp(640)
            maxLines = 4
            ellipsize = TextUtils.TruncateAt.END
            gravity = Gravity.CENTER
            setLineSpacing(0f, 1.1f)
            visibility = View.GONE
        }

        container.addView(header)
        container.addView(
            body,
            LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(6) },
        )

        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
            y = dp(24)
            setTitle("Assistant")
        }

        try {
            windowManager.addView(container, params)
        } catch (_: Exception) {
            return@post
        }
        root = container
        icon = iconView
        status = label
        detail = body
    }

    fun setState(text: String) = main.post {
        status?.text = text
    }

    /** listening / thinking / speaking / error / idle */
    fun setIcon(mode: String) = main.post {
        icon?.setMode(mode)
    }

    /** Smoothed mic level (0..1); call at mic-chunk rate while listening. */
    fun setLevel(value: Float) = main.post {
        icon?.setLevel(value)
    }

    fun setDetail(text: String) = main.post {
        hideDetail?.let { main.removeCallbacks(it) }
        hideDetail = null
        val body = detail ?: return@post
        if (text.isBlank()) {
            body.text = ""
            body.visibility = View.GONE
        } else {
            body.text = text
            body.visibility = View.VISIBLE
        }
    }

    fun clearDetail() = main.post {
        hideDetail?.let { main.removeCallbacks(it) }
        hideDetail = null
        detail?.text = ""
        detail?.visibility = View.GONE
    }

    /** Leaves the last reply on screen briefly, then returns to the compact idle pill. */
    fun clearDetailSoon(delayMs: Long = 6000) = main.post {
        hideDetail?.let { main.removeCallbacks(it) }
        val runnable = Runnable {
            detail?.text = ""
            detail?.visibility = View.GONE
            hideDetail = null
        }
        hideDetail = runnable
        main.postDelayed(runnable, delayMs)
    }

    /** Call when the first reply audio chunk starts playing. */
    fun beginSpeech() = main.post {
        speechStartMs = SystemClock.elapsedRealtime()
        speechDurationSec = 0f
        lastBoldSpan = null
        main.removeCallbacks(speechTick)
        main.postDelayed(speechTick, 100)
    }

    /** Actual audio duration, known once all chunks have arrived. */
    fun setSpeechDuration(seconds: Float) = main.post {
        if (seconds > 0f) speechDurationSec = seconds
    }

    fun endSpeech() = main.post {
        main.removeCallbacks(speechTick)
        val body = detail ?: return@post
        val text = body.text?.toString().orEmpty()
        body.text = text
        lastBoldSpan = null
    }

    private fun highlightCurrentWord() {
        val body = detail ?: return
        val text = body.text?.toString().orEmpty()
        if (text.isEmpty()) return
        val elapsed = (SystemClock.elapsedRealtime() - speechStartMs) / 1000f
        // Before the true duration is known, estimate from length
        // (~14 chars/sec); anchored exactly once audio_end reports totals.
        val duration = if (speechDurationSec > 0f) speechDurationSec else text.length / 14f
        if (duration <= 0f) return
        val frac = (elapsed / duration).coerceIn(0f, 1f)
        val at = min((text.length * frac).toInt(), text.length - 1)
        var start = at
        while (start > 0 && text[start - 1] != ' ' && text[start - 1] != '\n') start--
        var end = at
        while (end < text.length && text[end] != ' ' && text[end] != '\n') end++
        if (start == end) return
        val span = start to end
        if (span == lastBoldSpan) return
        lastBoldSpan = span
        val styled = SpannableString(text)
        styled.setSpan(
            StyleSpan(Typeface.BOLD),
            start, end,
            SpannableString.SPAN_EXCLUSIVE_EXCLUSIVE,
        )
        styled.setSpan(
            ForegroundColorSpan(0xFF7DD3FC.toInt()),
            start, end,
            SpannableString.SPAN_EXCLUSIVE_EXCLUSIVE,
        )
        body.text = styled
    }

    fun remove() = main.post {
        hideDetail?.let { main.removeCallbacks(it) }
        hideDetail = null
        main.removeCallbacks(speechTick)
        root?.let {
            try {
                windowManager.removeView(it)
            } catch (_: Exception) {
                // Already detached.
            }
        }
        root = null
        icon = null
        status = null
        detail = null
    }

    private fun dp(value: Int): Int = TypedValue.applyDimension(
        TypedValue.COMPLEX_UNIT_DIP,
        value.toFloat(),
        appContext.resources.displayMetrics,
    ).toInt()
}
