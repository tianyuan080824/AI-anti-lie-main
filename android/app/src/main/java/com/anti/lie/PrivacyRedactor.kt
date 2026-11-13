package com.anti.lie

import java.math.BigDecimal

object PrivacyRedactor {
    private val protected = Regex(
        """<(?:PHONE|CARD|ID)>|<AMOUNT:[0-9]+(?:\.[0-9]+)?>|(?:[A-Za-z][A-Za-z0-9+.\-]*://|www\.)[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+|(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}(?::[0-9]{1,5})?/[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*|(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}:[0-9]{1,5}""",
        RegexOption.IGNORE_CASE,
    )
    private val identity = Regex("(?<![0-9])([0-9]{17}[0-9Xx])(?![0-9])")
    private val card = Regex("(?<![0-9])([0-9]{16,19})(?![0-9])")
    private val phone = Regex("(?:\\+(?:00)?|(?<![0-9])00)86[ \\-－\\u3000]?1[3-9][0-9]{9}(?![0-9])|(?<![0-9])1[3-9][0-9]{9}(?![0-9])")
    private val number = "[0-9]{1,3}(?:,[0-9]{3})+(?:\\.[0-9]+)?|[0-9]+(?:\\.[0-9]+)?"
    private val currencyAmount = Regex("(?:[¥￥\$＄]|RMB|CNY) ?($number)([万亿千])?(?:元|块钱|块)?", RegexOption.IGNORE_CASE)
    private val unitAmount = Regex("(?<![0-9.])($number) ?(?:(万亿)(?:元|块钱|块)?|(千)(?:元|块钱|块)|元|块钱|块)")
    private val fullWidthPoint = Regex("(?<=[0-9])．(?=[0-9])")
    private val fullWidthIdX = Regex("(?<![0-9])([0-9]{17})([Ｘｘ])")
    private val multipliers = mapOf("万" to 10_000L, "亿" to 100_000_000L, "千" to 1_000L)

    fun redact(input: String): String {
        var text = input.map { char ->
            when (char) {
                in '０'..'９' -> ('0'.code + char.code - '０'.code).toChar()
                '＋' -> '+'
                else -> char
            }
        }.joinToString("")
        text = fullWidthPoint.replace(text, ".")
        text = fullWidthIdX.replace(text) { "${it.groupValues[1]}${if (it.groupValues[2] == "Ｘ") "X" else "x"}" }

        val saved = mutableListOf<String>()
        text = protected.replace(text) {
            saved += it.value
            "__KEEP_${saved.lastIndex}__"
        }
        while (true) {
            val next = redactOnce(text)
            if (next == text) break
            text = next
        }
        return Regex("__KEEP_([0-9]+)__").replace(text) { match ->
            saved[match.groupValues[1].toInt()]
        }
    }

    private fun redactOnce(value: String): String {
        var text = identity.replace(value) { match ->
            if (validIdentity(match.groupValues[1])) "<ID>" else match.value
        }
        text = card.replace(text) { match ->
            if (validLuhn(match.groupValues[1])) "<CARD>" else match.value
        }
        text = phone.replace(text, "<PHONE>")
        text = currencyAmount.replace(text) { match -> amount(match.groupValues[1], match.groupValues[2]) }
        return unitAmount.replace(text) { match ->
            val multiplier = match.groupValues[2].ifEmpty { match.groupValues[3] }
            amount(match.groupValues[1], multiplier)
        }
    }

    private fun amount(rawNumber: String, multiplier: String): String {
        val value = BigDecimal(rawNumber.replace(",", ""))
            .multiply(BigDecimal(multipliers[multiplier] ?: 1L))
            .stripTrailingZeros()
        return "<AMOUNT:${value.toPlainString()}>"
    }

    private fun validIdentity(value: String): Boolean {
        val weights = intArrayOf(7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
        val checks = "10X98765432"
        val sum = value.take(17).mapIndexed { index, char -> (char - '0') * weights[index] }.sum()
        return checks[sum % 11] == value.last().uppercaseChar()
    }

    private fun validLuhn(value: String): Boolean {
        val sum = value.reversed().mapIndexed { index, char ->
            var digit = char - '0'
            if (index % 2 == 1) {
                digit *= 2
                if (digit > 9) digit -= 9
            }
            digit
        }.sum()
        return sum % 10 == 0
    }
}