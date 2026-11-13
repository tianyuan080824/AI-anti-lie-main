package com.anti.lie

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.speech.tts.TextToSpeech
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale
import java.util.UUID

private object Palette {
    val canvas = Color(0xFFFFF8F0)
    val surface = Color(0xFFFFFEFC)
    val ink = Color(0xFF35251D)
    val muted = Color(0xFF76675D)
    val accent = Color(0xFF8C4325)
    val accentWash = Color(0xFFFBE5D1)
    val border = Color(0xFFE7D8CB)
    val danger = Color(0xFFB3261E)
    val dangerWash = Color(0xFFFFE7E3)
    val green = Color(0xFF37624A)
    val greenWash = Color(0xFFE5F0E7)
    val blue = Color(0xFF365F77)
}

class MainActivity : ComponentActivity(), TextToSpeech.OnInitListener {
    private var tts: TextToSpeech? = null
    private var ttsReady = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        tts = TextToSpeech(this, this)
        setContent { AntiFraudApp(::speak, ::stopSpeaking) }
    }

    override fun onInit(status: Int) {
        ttsReady = status == TextToSpeech.SUCCESS
        if (ttsReady) tts?.language = Locale.SIMPLIFIED_CHINESE
    }

    private fun speak(text: String) {
        if (ttsReady) tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "anti-fraud-reading")
    }

    private fun stopSpeaking() {
        tts?.stop()
    }

    override fun onDestroy() {
        tts?.stop()
        tts?.shutdown()
        super.onDestroy()
    }
}

@Composable
private fun AntiFraudApp(speak: (String) -> Unit, stopSpeaking: () -> Unit) {
    val context = LocalContext.current
    val store = remember { context.getSharedPreferences("anti_fraud_preferences", 0) }
    var baseUrl by remember { mutableStateOf(store.getString("base_url", "http://127.0.0.1:8000") ?: "http://127.0.0.1:8000") }
    var token by remember { mutableStateOf(store.getString("token", "dev-token") ?: "dev-token") }
    var deviceId by remember {
        mutableStateOf(store.getString("device_id", null) ?: UUID.randomUUID().toString().also {
            store.edit().putString("device_id", it).apply()
        })
    }
    var fontScale by remember { mutableIntStateOf(store.getInt("font_scale", 1)) }
    var simpleLayout by remember { mutableStateOf(store.getBoolean("simple_layout", true)) }
    var selectedTab by remember { mutableIntStateOf(0) }
    var onboardingStep by remember { mutableIntStateOf(if (store.getBoolean("setup_done", false)) -1 else 0) }
    var input by remember { mutableStateOf("") }
    var result by remember { mutableStateOf<JSONObject?>(null) }
    var progress by remember { mutableStateOf<JSONObject?>(null) }
    var busy by remember { mutableStateOf(false) }
    var serviceMessage by remember { mutableStateOf("正在检查服务") }
    var mockMode by remember { mutableStateOf<Boolean?>(null) }
    var errorMessage by remember { mutableStateOf<String?>(null) }
    var feedbackMessage by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    val api = remember(baseUrl, token, deviceId) { AntiFraudApi(baseUrl, token, deviceId) }
    val scale = listOf(1f, 1.2f, 1.42f)[fontScale.coerceIn(0, 2)]

    LaunchedEffect(baseUrl, token) {
        kotlinx.coroutines.delay(450)
        serviceMessage = "正在检查服务"
        try {
            val health = api.health()
            mockMode = health.optBoolean("mock", false)
            serviceMessage = if (health.optString("status") == "ok") "服务已连接" else "服务暂不可用"
        } catch (_: Exception) {
            mockMode = null
            serviceMessage = "尚未连接服务"
        }
    }

    MaterialTheme(colorScheme = lightColorScheme(
        primary = Palette.accent,
        onPrimary = Color.White,
        secondary = Palette.green,
        background = Palette.canvas,
        surface = Palette.surface,
        onSurface = Palette.ink,
        error = Palette.danger,
    )) {
        Surface(modifier = Modifier.fillMaxSize(), color = Palette.canvas) {
            if (onboardingStep >= 0) {
                FirstRunSetup(
                    step = onboardingStep,
                    fontScale = fontScale,
                    simpleLayout = simpleLayout,
                    onFont = { fontScale = it; store.edit().putInt("font_scale", it).apply() },
                    onSimple = { simpleLayout = it; store.edit().putBoolean("simple_layout", it).apply() },
                    onSpeak = { speak("别着急，我们一起核实一下。你可以随时调整阅读和声音设置。") },
                    onNext = {
                        if (onboardingStep < 2) onboardingStep += 1
                        else {
                            onboardingStep = -1
                            store.edit().putBoolean("setup_done", true).apply()
                        }
                    },
                    onSkip = {
                        onboardingStep = -1
                        store.edit().putBoolean("setup_done", true).apply()
                    },
                    scale = scale,
                )
            } else {
                Scaffold(
                    containerColor = Palette.canvas,
                    bottomBar = {
                        NavigationBar(
                            containerColor = Palette.surface,
                            modifier = Modifier.windowInsetsPadding(WindowInsets.navigationBars),
                        ) {
                            listOf("首页", "演练", "家庭", "设置").forEachIndexed { index, label ->
                                NavigationBarItem(
                                    selected = selectedTab == index,
                                    onClick = { selectedTab = index },
                                    icon = { Text(listOf("看", "练", "家", "设")[index], fontSize = 14.sp) },
                                    label = { Text(label, fontSize = (12 * scale).sp) },
                                )
                            }
                        }
                    },
                ) { insets ->
                    when (selectedTab) {
                        0 -> HomeScreen(
                            modifier = Modifier.padding(insets),
                            input = input,
                            onInput = { input = it },
                            busy = busy,
                            progress = progress,
                            errorMessage = errorMessage,
                            serviceMessage = serviceMessage,
                            mockMode = mockMode,
                            scale = scale,
                            simpleLayout = simpleLayout,
                            onAnalyze = {
                                errorMessage = null
                                result = null
                                feedbackMessage = null
                                busy = true
                                scope.launch {
                                    try {
                                        result = api.check(input) { current -> progress = current }
                                    } catch (error: Exception) {
                                        errorMessage = error.message ?: "鉴定没有完成，请重试"
                                    } finally {
                                        busy = false
                                    }
                                }
                            },
                            onOpenSettings = { selectedTab = 3 },
                        )
                        1 -> PlaceholderScreen("反诈演练", "演练内容尚未接入服务端。", scale, Modifier.padding(insets))
                        2 -> PlaceholderScreen("家庭协作", "当前版本不共享聊天内容，也不提供远程监控。", scale, Modifier.padding(insets))
                        else -> SettingsScreen(
                            modifier = Modifier.padding(insets),
                            baseUrl = baseUrl,
                            token = token,
                            fontScale = fontScale,
                            simpleLayout = simpleLayout,
                            serviceMessage = serviceMessage,
                            onBaseUrl = { baseUrl = it; store.edit().putString("base_url", it).apply() },
                            onToken = { token = it; store.edit().putString("token", it).apply() },
                            onFont = { fontScale = it; store.edit().putInt("font_scale", it).apply() },
                            onSimple = { simpleLayout = it; store.edit().putBoolean("simple_layout", it).apply() },
                            onTestVoice = { speak("语速试听。别着急，我们一起核实一下。") },
                            onStopVoice = stopSpeaking,
                            scale = scale,
                        )
                    }
                    if (result != null && selectedTab == 0) {
                        ReportDialog(
                            response = result!!,
                            sourceText = input,
                            scale = scale,
                            feedbackMessage = feedbackMessage,
                            onClose = { result = null },
                            onSpeak = { report ->
                                speak(listOf(report.optString("headline"), report.optString("summary"), report.optString("next_move")).filter(String::isNotBlank).joinToString("。"))
                            },
                            onStop = stopSpeaking,
                            onDial = { number ->
                                context.startActivity(Intent(Intent.ACTION_DIAL, Uri.parse("tel:$number")))
                            },
                            onFeedback = { label ->
                                scope.launch {
                                    try {
                                        api.sendFeedback(result!!.getString("check_id"), label)
                                        feedbackMessage = "已收到，谢谢你的反馈"
                                    } catch (error: Exception) {
                                        feedbackMessage = error.message ?: "反馈未能提交"
                                    }
                                }
                            },
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun HomeScreen(
    modifier: Modifier,
    input: String,
    onInput: (String) -> Unit,
    busy: Boolean,
    progress: JSONObject?,
    errorMessage: String?,
    serviceMessage: String,
    mockMode: Boolean?,
    scale: Float,
    simpleLayout: Boolean,
    onAnalyze: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    LazyColumn(
        modifier = modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(start = 22.dp, top = 24.dp, end = 22.dp, bottom = 28.dp),
        verticalArrangement = Arrangement.spacedBy(18.dp),
    ) {
        item {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("反诈陪伴助手", fontSize = (15 * scale).sp, color = Palette.muted, fontWeight = FontWeight.Medium)
                Text("别着急，\n我们一起看一看。", fontSize = (30 * scale).sp, lineHeight = (39 * scale).sp, color = Palette.ink, fontWeight = FontWeight.SemiBold)
                Text("手动粘贴一段文字，先核实再决定。", fontSize = (17 * scale).sp, lineHeight = (25 * scale).sp, color = Palette.muted)
            }
        }
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(9.dp).background(if (serviceMessage == "服务已连接") Palette.green else Palette.muted, CircleShape))
                Text(serviceMessage, fontSize = (14 * scale).sp, color = Palette.muted)
                if (mockMode == true) {
                    Surface(color = Palette.accentWash, shape = RoundedCornerShape(50)) {
                        Text("模拟数据", modifier = Modifier.padding(horizontal = 10.dp, vertical = 5.dp), color = Palette.accent, fontSize = (12 * scale).sp, fontWeight = FontWeight.SemiBold)
                    }
                }
                Spacer(Modifier.weight(1f))
                TextButton(onClick = onOpenSettings) { Text("连接设置", fontSize = (13 * scale).sp) }
            }
        }
        item {
            Card(
                shape = RoundedCornerShape(18.dp),
                colors = CardDefaults.cardColors(containerColor = Palette.surface),
                border = androidx.compose.foundation.BorderStroke(1.dp, Palette.border),
            ) {
                Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
                    Text("需要核实的内容", fontSize = (18 * scale).sp, fontWeight = FontWeight.SemiBold)
                    OutlinedTextField(
                        value = input,
                        onValueChange = onInput,
                        modifier = Modifier.fillMaxWidth(),
                        minLines = 5,
                        maxLines = 9,
                        placeholder = { Text("粘贴对方发来的文字或链接", fontSize = (16 * scale).sp, lineHeight = (24 * scale).sp) },
                        supportingText = { Text("只会分析你主动提交的内容，上传前会在本机脱敏。", fontSize = (12 * scale).sp) },
                        keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Sentences, keyboardType = KeyboardType.Text),
                        shape = RoundedCornerShape(12.dp),
                    )
                    Button(
                        onClick = onAnalyze,
                        enabled = !busy && input.isNotBlank(),
                        modifier = Modifier.fillMaxWidth().height(58.dp),
                        shape = RoundedCornerShape(16.dp),
                        colors = ButtonDefaults.buttonColors(containerColor = Palette.accent),
                    ) {
                        if (busy) {
                            CircularProgressIndicator(modifier = Modifier.size(20.dp), color = Color.White, strokeWidth = 2.dp)
                            Spacer(Modifier.width(10.dp))
                            Text("正在核实", fontSize = (17 * scale).sp, fontWeight = FontWeight.SemiBold)
                        } else Text("帮我看看", fontSize = (18 * scale).sp, fontWeight = FontWeight.SemiBold)
                    }
                }
            }
        }
        if (busy) {
            item { AnalyzerProgress(progress, scale) }
        }
        if (!errorMessage.isNullOrBlank()) {
            item {
                MessagePanel("这次没有完成", errorMessage, Palette.dangerWash, Palette.danger, scale)
            }
        }
        if (!simpleLayout) {
            item {
                Card(shape = RoundedCornerShape(16.dp), colors = CardDefaults.cardColors(containerColor = Palette.accentWash)) {
                    Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(7.dp)) {
                        Text("当前能力", fontSize = (16 * scale).sp, fontWeight = FontWeight.SemiBold, color = Palette.ink)
                        Text("分析文字和其中的链接。结果用于辅助核实，不替你作决定。", fontSize = (15 * scale).sp, lineHeight = (23 * scale).sp, color = Palette.ink)
                    }
                }
            }
        }
        item {
            Text("遇到转账、验证码或安装陌生应用的要求，可以先暂停，通过你已知的联系方式核实。", fontSize = (14 * scale).sp, lineHeight = (21 * scale).sp, color = Palette.muted)
        }
    }
}

@Composable
private fun AnalyzerProgress(progress: JSONObject?, scale: Float) {
    val names = mapOf("script" to "话术", "link" to "链接", "cases" to "相似案例", "deepfake" to "媒体", "summary" to "汇总")
    val analyzers = progress?.optJSONArray("analyzers") ?: JSONArray()
    Card(shape = RoundedCornerShape(16.dp), colors = CardDefaults.cardColors(containerColor = Palette.surface), border = androidx.compose.foundation.BorderStroke(1.dp, Palette.border)) {
        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("正在逐项分析", fontSize = (16 * scale).sp, fontWeight = FontWeight.SemiBold)
            for (index in 0 until analyzers.length()) {
                val analyzer = analyzers.optJSONObject(index) ?: continue
                val name = names[analyzer.optString("name")] ?: analyzer.optString("name")
                val status = when (analyzer.optString("status")) {
                    "done" -> "已完成"
                    "running" -> "正在分析"
                    "failed" -> "暂不可用"
                    "skipped" -> "未涉及"
                    else -> "等待中"
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text(name, fontSize = (14 * scale).sp, color = Palette.ink)
                    Text(status, fontSize = (14 * scale).sp, color = if (status == "已完成") Palette.green else Palette.muted)
                }
            }
        }
    }
}

@Composable
private fun ReportDialog(
    response: JSONObject,
    sourceText: String,
    scale: Float,
    feedbackMessage: String?,
    onClose: () -> Unit,
    onSpeak: (JSONObject) -> Unit,
    onStop: () -> Unit,
    onDial: (String) -> Unit,
    onFeedback: (String) -> Unit,
) {
    val report = response.optJSONObject("report")
    if (report == null) {
        androidx.compose.material3.AlertDialog(
            onDismissRequest = onClose,
            title = { Text("暂时没有报告") },
            text = { Text("服务没有返回可展示的报告。") },
            confirmButton = { TextButton(onClick = onClose) { Text("关闭") } },
        )
        return
    }
    val verdict = report.optString("verdict", "inconclusive")
    val (label, color, wash) = when (verdict) {
        "high_risk" -> Triple("高风险", Palette.danger, Palette.dangerWash)
        "suspicious" -> Triple("需要留意", Palette.accent, Palette.accentWash)
        "low_risk" -> Triple("暂未发现明显风险", Palette.green, Palette.greenWash)
        else -> Triple("目前无法判断", Palette.muted, Color(0xFFF0ECE7))
    }
    val score = report.optInt("risk_score", 0).coerceIn(0, 100)
    androidx.compose.ui.window.Dialog(onDismissRequest = onClose) {
        Surface(shape = RoundedCornerShape(20.dp), color = Palette.canvas, modifier = Modifier.fillMaxWidth().fillMaxSize(0.94f)) {
            LazyColumn(
                contentPadding = androidx.compose.foundation.layout.PaddingValues(20.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp),
            ) {
                item {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                        Text("鉴定报告", fontSize = (22 * scale).sp, fontWeight = FontWeight.Bold, color = Palette.ink)
                        TextButton(onClick = onClose) { Text("关闭", fontSize = (15 * scale).sp) }
                    }
                    if (response.optString("status") == "succeeded") {
                        Text("仅供参考 · 结果不代表安全保证", fontSize = (13 * scale).sp, color = Palette.muted)
                    }
                }
                item {
                    Card(shape = RoundedCornerShape(18.dp), colors = CardDefaults.cardColors(containerColor = wash), border = androidx.compose.foundation.BorderStroke(1.dp, color.copy(alpha = 0.18f))) {
                        Column(Modifier.fillMaxWidth().padding(18.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                            Text(label, color = color, fontSize = (22 * scale).sp, fontWeight = FontWeight.Bold)
                            Text(report.optString("headline", label), color = Palette.ink, fontSize = (18 * scale).sp, lineHeight = (27 * scale).sp, fontWeight = FontWeight.SemiBold)
                            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                                Text("风险参考分数  $score", fontSize = (14 * scale).sp, color = Palette.muted)
                            }
                            LinearProgressIndicator(progress = score / 100f, modifier = Modifier.fillMaxWidth(), color = color, trackColor = Color.White.copy(alpha = 0.75f))
                        }
                    }
                }
                item {
                    Text("先做什么", fontSize = (18 * scale).sp, color = Palette.ink, fontWeight = FontWeight.Bold)
                    Text(report.optString("summary"), fontSize = (16 * scale).sp, lineHeight = (25 * scale).sp, color = Palette.ink)
                    if (report.optString("next_move").isNotBlank()) Text(report.optString("next_move"), modifier = Modifier.padding(top = 8.dp), fontSize = (15 * scale).sp, lineHeight = (23 * scale).sp, color = Palette.muted)
                    TextButton(onClick = { onSpeak(report) }) { Text("朗读报告", fontSize = (14 * scale).sp) }
                    TextButton(onClick = onStop) { Text("停止朗读", fontSize = (14 * scale).sp) }
                }
                item {
                    SectionTitle("为什么这样判断", scale)
                    val evidence = report.optJSONArray("evidence") ?: JSONArray()
                    if (evidence.length() == 0) {
                        Text("当前报告没有可引用的具体证据。", fontSize = (15 * scale).sp, color = Palette.muted)
                    } else {
                        for (index in 0 until evidence.length()) {
                            val item = evidence.optJSONObject(index) ?: continue
                            Card(shape = RoundedCornerShape(12.dp), colors = CardDefaults.cardColors(containerColor = Palette.surface), border = androidx.compose.foundation.BorderStroke(1.dp, Palette.border), modifier = Modifier.padding(top = 8.dp)) {
                                Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(7.dp)) {
                                    val quote = item.optString("quote")
                                    val sourceIndex = item.optString("line_id").substringAfterLast("-", "").toIntOrNull()
                                    val sourceLine = sourceText.lineSequence().map(String::trim).filter(String::isNotEmpty)
                                        .toList().getOrNull(sourceIndex ?: -1)?.let(PrivacyRedactor::redact)
                                    val quoteStart = sourceLine?.indexOf(quote) ?: -1
                                    if (sourceLine != null && quoteStart >= 0) {
                                        Text(
                                            buildAnnotatedString {
                                                append(sourceLine.substring(0, quoteStart))
                                                withStyle(SpanStyle(background = Palette.accentWash, fontWeight = FontWeight.Bold)) { append(quote) }
                                                append(sourceLine.substring(quoteStart + quote.length))
                                            },
                                            fontSize = (16 * scale).sp,
                                            lineHeight = (25 * scale).sp,
                                            color = Palette.ink,
                                        )
                                    } else {
                                        Text("“$quote”", fontSize = (16 * scale).sp, lineHeight = (25 * scale).sp, color = Palette.ink, fontWeight = FontWeight.SemiBold)
                                        Text("原文中未能定位该片段，未添加高亮。", fontSize = (12 * scale).sp, color = Palette.muted)
                                    }
                                    Text(item.optString("explanation"), fontSize = (14 * scale).sp, lineHeight = (21 * scale).sp, color = Palette.muted)
                                    Text("来源：${if (item.optString("source") == "rule") "规则" else "模型"} · ${item.optString("line_id")}", fontSize = (12 * scale).sp, color = Palette.muted)
                                }
                            }
                        }
                    }
                }
                item {
                    SectionTitle("建议", scale)
                    val advice = report.optJSONArray("advice") ?: JSONArray()
                    for (index in 0 until advice.length()) {
                        Row(Modifier.fillMaxWidth().padding(vertical = 5.dp), verticalAlignment = Alignment.Top) {
                            Text("${index + 1}.", fontSize = (15 * scale).sp, color = Palette.accent, fontWeight = FontWeight.Bold)
                            Spacer(Modifier.width(8.dp))
                            Text(advice.optString(index), fontSize = (15 * scale).sp, lineHeight = (23 * scale).sp, color = Palette.ink)
                        }
                    }
                    val actions = report.optJSONArray("actions") ?: JSONArray()
                    for (index in 0 until actions.length()) {
                        val action = actions.optJSONObject(index) ?: continue
                        if (action.optString("type") == "dial") {
                            OutlinedButton(onClick = { onDial(action.optString("value")) }, modifier = Modifier.fillMaxWidth()) {
                                Text(action.optString("label"), fontSize = (15 * scale).sp)
                            }
                        }
                    }
                }
                item {
                    SectionTitle("你的判断也很重要", scale)
                    Text("这次结果符合你的了解吗？", fontSize = (15 * scale).sp, color = Palette.ink)
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        listOf("诈骗" to "scam", "不是诈骗" to "not_scam", "不确定" to "unsure").forEach { (text, value) ->
                            OutlinedButton(onClick = { onFeedback(value) }, modifier = Modifier.weight(1f)) { Text(text, fontSize = (12 * scale).sp) }
                        }
                    }
                    if (!feedbackMessage.isNullOrBlank()) Text(feedbackMessage, modifier = Modifier.padding(top = 6.dp), color = Palette.green, fontSize = (13 * scale).sp)
                }
                item {
                    val disclaimer = report.optString("disclaimer")
                    if (disclaimer.isNotBlank()) Text(disclaimer, fontSize = (13 * scale).sp, lineHeight = (20 * scale).sp, color = Palette.muted)
                    Spacer(Modifier.height(8.dp))
                }
            }
        }
    }
}

@Composable
private fun SettingsScreen(
    modifier: Modifier,
    baseUrl: String,
    token: String,
    fontScale: Int,
    simpleLayout: Boolean,
    serviceMessage: String,
    onBaseUrl: (String) -> Unit,
    onToken: (String) -> Unit,
    onFont: (Int) -> Unit,
    onSimple: (Boolean) -> Unit,
    onTestVoice: () -> Unit,
    onStopVoice: () -> Unit,
    scale: Float,
) {
    LazyColumn(
        modifier = modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(22.dp),
        verticalArrangement = Arrangement.spacedBy(18.dp),
    ) {
        item {
            Text("设置", fontSize = (28 * scale).sp, fontWeight = FontWeight.Bold, color = Palette.ink)
            Text("阅读和连接方式可随时更改。", fontSize = (16 * scale).sp, color = Palette.muted)
        }
        item {
            SectionTitle("阅读与声音", scale)
            listOf("标准", "大字", "特大").forEachIndexed { index, label ->
                Row(verticalAlignment = Alignment.CenterVertically) {
                    RadioButton(selected = fontScale == index, onClick = { onFont(index) })
                    Text(label, fontSize = (16 * scale).sp, color = Palette.ink)
                }
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("简洁布局", modifier = Modifier.weight(1f), fontSize = (16 * scale).sp, color = Palette.ink)
                androidx.compose.material3.Switch(checked = simpleLayout, onCheckedChange = onSimple)
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = onTestVoice) { Text("语速试听") }
                OutlinedButton(onClick = onStopVoice) { Text("停止朗读") }
            }
        }
        item {
            SectionTitle("服务连接", scale)
            Text("当前状态：$serviceMessage", fontSize = (14 * scale).sp, color = Palette.muted)
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = baseUrl,
                onValueChange = onBaseUrl,
                modifier = Modifier.fillMaxWidth(),
                label = { Text("服务地址") },
                supportingText = { Text("USB 调试可用 http://127.0.0.1:8000；连接热点时填笔记本地址。") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, capitalization = KeyboardCapitalization.None),
            )
            OutlinedTextField(
                value = token,
                onValueChange = onToken,
                modifier = Modifier.fillMaxWidth(),
                label = { Text("访问令牌") },
                singleLine = true,
            )
            Text("令牌只保存在本机，不会写入报告。", fontSize = (12 * scale).sp, color = Palette.muted)
        }
        item {
            SectionTitle("隐私", scale)
            Text("文本先在本机脱敏后再发送。不会自动读取聊天、短信或剪贴板；媒体上传需要单独确认（本版暂未接入媒体选择）。", fontSize = (15 * scale).sp, lineHeight = (23 * scale).sp, color = Palette.ink)
        }
    }
}

@Composable
private fun FirstRunSetup(
    step: Int,
    fontScale: Int,
    simpleLayout: Boolean,
    onFont: (Int) -> Unit,
    onSimple: (Boolean) -> Unit,
    onSpeak: () -> Unit,
    onNext: () -> Unit,
    onSkip: () -> Unit,
    scale: Float,
) {
    Column(
        modifier = Modifier.fillMaxSize().padding(horizontal = 24.dp, vertical = 30.dp),
        verticalArrangement = Arrangement.SpaceBetween,
    ) {
        Column(verticalArrangement = Arrangement.spacedBy(18.dp)) {
            Text("开始之前", fontSize = (15 * scale).sp, color = Palette.muted)
            Text(listOf("选择阅读字号", "试听朗读与布局", "预览外观")[step], fontSize = (28 * scale).sp, fontWeight = FontWeight.Bold, color = Palette.ink)
            when (step) {
                0 -> {
                    Text("之后也可以在设置中调整。", fontSize = (16 * scale).sp, color = Palette.muted)
                    listOf("标准", "大字", "特大").forEachIndexed { index, label ->
                        Card(
                            onClick = { onFont(index) },
                            shape = RoundedCornerShape(14.dp),
                            colors = CardDefaults.cardColors(containerColor = if (fontScale == index) Palette.accentWash else Palette.surface),
                            border = androidx.compose.foundation.BorderStroke(1.dp, if (fontScale == index) Palette.accent else Palette.border),
                        ) {
                            Row(Modifier.fillMaxWidth().padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
                                RadioButton(selected = fontScale == index, onClick = { onFont(index) })
                                Text(label, fontSize = (18 + index * 3 * scale).sp, color = Palette.ink)
                            }
                        }
                    }
                }
                1 -> {
                    Text("默认不会自动播放报告或敏感内容。", fontSize = (16 * scale).sp, color = Palette.muted)
                    Button(onClick = onSpeak, shape = RoundedCornerShape(14.dp)) { Text("试听：别着急，我们一起核实一下") }
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("简洁布局", modifier = Modifier.weight(1f), fontSize = (17 * scale).sp, color = Palette.ink)
                        androidx.compose.material3.Switch(checked = simpleLayout, onCheckedChange = onSimple)
                    }
                }
                else -> {
                    Card(shape = RoundedCornerShape(20.dp), colors = CardDefaults.cardColors(containerColor = Palette.accentWash)) {
                        Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                            Text("别着急，\n我们一起看一看。", fontSize = (25 * scale).sp, lineHeight = (33 * scale).sp, fontWeight = FontWeight.SemiBold, color = Palette.ink)
                            Text("暖阳客厅 · 清晰大字 · 手动鉴诈", fontSize = (15 * scale).sp, color = Palette.muted)
                        }
                    }
                }
            }
        }
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = onNext, modifier = Modifier.fillMaxWidth().height(56.dp), shape = RoundedCornerShape(16.dp)) {
                Text(if (step == 2) "完成设置" else "继续", fontSize = (17 * scale).sp)
            }
            TextButton(onClick = onSkip, modifier = Modifier.align(Alignment.CenterHorizontally)) { Text("跳过设置") }
        }
    }
}

@Composable
private fun PlaceholderScreen(title: String, description: String, scale: Float, modifier: Modifier = Modifier) {
    Column(modifier.fillMaxSize().padding(24.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Text(title, fontSize = (28 * scale).sp, fontWeight = FontWeight.Bold, color = Palette.ink)
        Text(description, fontSize = (17 * scale).sp, lineHeight = (26 * scale).sp, color = Palette.muted)
        MessagePanel("当前版本", "此页面暂未接入，不会展示虚构的共享状态或训练结果。", Palette.accentWash, Palette.ink, scale)
    }
}

@Composable
private fun MessagePanel(title: String, body: String, background: Color, foreground: Color, scale: Float) {
    Card(shape = RoundedCornerShape(14.dp), colors = CardDefaults.cardColors(containerColor = background)) {
        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
            Text(title, fontSize = (16 * scale).sp, color = foreground, fontWeight = FontWeight.SemiBold)
            Text(body, fontSize = (14 * scale).sp, lineHeight = (21 * scale).sp, color = foreground)
        }
    }
}

@Composable
private fun SectionTitle(title: String, scale: Float) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(title, fontSize = (18 * scale).sp, color = Palette.ink, fontWeight = FontWeight.Bold)
        HorizontalDivider(color = Palette.border)
    }
}
