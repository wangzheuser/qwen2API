package toolcall

import (
	"encoding/json"
	"regexp"
	"strings"
)

var looseJSONReplacements = []struct {
	re   *regexp.Regexp
	repl string
}{
	{regexp.MustCompile(`(?is)"name="\s*`), `"name": "`},
	{regexp.MustCompile(`(?is)"name=([^",}\s]+)"`), `"name": "$1"`},
	{regexp.MustCompile(`(?is)"name=([^",}\s]+)`), `"name": "$1"`},
	{regexp.MustCompile(`(?is)"name\s*=\s*"`), `"name": "`},
	{regexp.MustCompile(`(?is)"(name|input|arguments|args|parameters|tool|tool_name|function_name)"\s*=\s*`), `"$1": `},
	{regexp.MustCompile(`(?is)([{,]\s*)(name|input|arguments|args|parameters|tool|tool_name|function_name)\s*:`), `$1"$2":`},
}

func parseJSONToolCalls(value any, allowed map[string]string) []ParsedToolCall {
	calls := []ParsedToolCall{}
	switch v := value.(type) {
	case map[string]any:
		if rawList, ok := v["tool_calls"].([]any); ok {
			for _, raw := range rawList {
				calls = append(calls, parseJSONToolCalls(raw, allowed)...)
			}
		}
		if rawList, ok := v["tools"].([]any); ok {
			for _, raw := range rawList {
				calls = append(calls, parseJSONToolCalls(raw, allowed)...)
			}
		}
		name := firstString(v["name"], v["tool"], v["tool_name"], v["function_name"])
		input := firstNonNil(v["input"], v["arguments"], v["args"], v["parameters"])
		if fn, ok := v["function"].(map[string]any); ok {
			if name == "" {
				name = firstString(fn["name"])
			}
			if input == nil {
				input = firstNonNil(fn["arguments"], fn["input"], fn["parameters"])
			}
		}
		if name = canonicalToolName(name, allowed); name != "" {
			calls = append(calls, ParsedToolCall{
				ID:    firstNonEmpty(firstString(v["id"], v["call_id"]), "call_"+randomID()[:12]),
				Name:  name,
				Input: NormalizeToolInput(input),
			})
		}
	case []any:
		for _, item := range v {
			calls = append(calls, parseJSONToolCalls(item, allowed)...)
		}
	}
	return calls
}

// ForEachJSONFragment visits standalone JSON objects or arrays embedded in text.
func ForEachJSONFragment(text string, visit func(any)) {
	normalized := stripJSONFence(text)
	incomplete := forEachBalancedJSON(normalized, func(candidate string) {
		decodeJSONCandidate(candidate, visit)
	})
	if incomplete != "" {
		if recovered := servicesRecoverJSONLike(incomplete); recovered != incomplete {
			decodeJSONCandidate(recovered, visit)
		}
	}
}

// forEachBalancedJSON finds outer JSON objects and arrays in one pass.
func forEachBalancedJSON(text string, visit func(string)) string {
	start := -1
	stack := make([]byte, 0, 16)
	inString := false
	escaped := false
	for i := 0; i < len(text); i++ {
		ch := text[i]
		if start < 0 {
			if ch == '{' || ch == '[' {
				start = i
				stack = append(stack[:0], matchingJSONClose(ch))
			}
			continue
		}
		if inString {
			if escaped {
				escaped = false
			} else if ch == '\\' {
				escaped = true
			} else if ch == '"' {
				inString = false
			}
			continue
		}
		switch ch {
		case '"':
			inString = true
		case '{', '[':
			stack = append(stack, matchingJSONClose(ch))
		case '}', ']':
			if len(stack) == 0 || stack[len(stack)-1] != ch {
				start = -1
				stack = stack[:0]
				continue
			}
			stack = stack[:len(stack)-1]
			if len(stack) == 0 {
				visit(text[start : i+1])
				start = -1
			}
		}
	}
	if start >= 0 {
		return text[start:]
	}
	return ""
}

// matchingJSONClose returns the closing delimiter for a JSON container.
func matchingJSONClose(open byte) byte {
	if open == '{' {
		return '}'
	}
	return ']'
}

// decodeJSONCandidate decodes exact JSON before trying the existing loose syntax repair.
func decodeJSONCandidate(candidate string, visit func(any)) bool {
	var value any
	if err := json.Unmarshal([]byte(candidate), &value); err == nil {
		visit(value)
		return true
	}
	if repaired := repairLooseJSON(candidate); repaired != candidate {
		if err := json.Unmarshal([]byte(repaired), &value); err == nil {
			visit(value)
			return true
		}
	}
	return false
}

func stripJSONFence(text string) string {
	stripped := strings.TrimSpace(text)
	if !strings.HasPrefix(stripped, "```") {
		return stripped
	}
	stripped = strings.TrimPrefix(stripped, "```json")
	stripped = strings.TrimPrefix(stripped, "```")
	stripped = strings.TrimSpace(stripped)
	if strings.HasSuffix(stripped, "```") {
		stripped = strings.TrimSpace(strings.TrimSuffix(stripped, "```"))
	}
	return stripped
}

func repairLooseJSON(text string) string {
	repaired := strings.TrimSpace(text)
	if repaired == "" {
		return repaired
	}
	for _, replacement := range looseJSONReplacements {
		repaired = replacement.re.ReplaceAllString(repaired, replacement.repl)
	}
	return repaired
}

func servicesRecoverJSONLike(text string) string {
	text = strings.TrimSpace(text)
	if text == "" {
		return text
	}
	openBraces := strings.Count(text, "{") - strings.Count(text, "}")
	openBrackets := strings.Count(text, "[") - strings.Count(text, "]")
	if openBraces <= 0 && openBrackets <= 0 {
		return text
	}
	var recovered strings.Builder
	recovered.Grow(len(text) + max(0, openBraces) + max(0, openBrackets))
	recovered.WriteString(text)
	recovered.WriteString(strings.Repeat("]", max(0, openBrackets)))
	recovered.WriteString(strings.Repeat("}", max(0, openBraces)))
	return recovered.String()
}
