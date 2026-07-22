package upstream

import "testing"

// TestBuildChatPayloadKeepsThinkingForQwen38Tools 验证工具桥不会关闭 qwen3.8 的必需思考模式。
func TestBuildChatPayloadKeepsThinkingForQwen38Tools(t *testing.T) {
	disabled := false
	payload := BuildChatPayload("chat", "qwen3.8-max-preview", "prompt", true, nil, "t2t", nil, &disabled, false)
	messages := payload["messages"].([]map[string]any)
	feature := messages[0]["feature_config"].(map[string]any)
	if feature["thinking_enabled"] != true || feature["thinking_mode"] != "Auto" {
		t.Fatalf("qwen3.8 tools must keep thinking enabled: %#v", feature)
	}
}
