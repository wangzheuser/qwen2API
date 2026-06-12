package main

import "testing"

func TestShouldForceToolContinuationAllowsGroundedBashMarker(t *testing.T) {
	req := StandardRequest{
		Prompt: stringsJoinForTest(
			"Human: create a file, verify it, then answer exactly TOOL_OK_123",
			"",
			"Assistant: <|QNML|tool_calls><|QNML|invoke name=\"Bash\"><|QNML|parameter name=\"command\"><![CDATA[python3 verify.py]]></|QNML|parameter></|QNML|invoke></|QNML|tool_calls>",
			"",
			"[Tool Result id=toolu_123]",
			"TOOL_OK_123",
			"[/Tool Result]",
			"",
			"[STATE NOTICE: MUST OBEY]",
			"The latest client message is a tool result, not a new user request.",
			"Assistant:",
		),
		Tools:                     []map[string]any{{"name": "Bash"}},
		ToolNames:                 []string{"Bash"},
		ToolEnabled:               true,
		LatestMessageIsToolResult: true,
	}
	result := CompletionResult{AnswerText: "TOOL_OK_123"}

	if shouldForceToolContinuation(req, result) {
		t.Fatal("grounded Bash marker should be accepted as final answer")
	}
}

func TestShouldForceToolContinuationStillRejectsGenericNarration(t *testing.T) {
	req := StandardRequest{
		Prompt: stringsJoinForTest(
			"Human: create a file, verify it, then answer exactly TOOL_OK_123",
			"",
			"Assistant: <|QNML|tool_calls><|QNML|invoke name=\"Write\"><|QNML|parameter name=\"file_path\"><![CDATA[probe.txt]]></|QNML|parameter></|QNML|invoke></|QNML|tool_calls>",
			"",
			"[Tool Result id=toolu_123]",
			"File created successfully at: probe.txt",
			"[/Tool Result]",
			"",
			"[STATE NOTICE: MUST OBEY]",
			"The latest client message is a tool result, not a new user request.",
			"Assistant:",
		),
		Tools:                     []map[string]any{{"name": "Write"}},
		ToolNames:                 []string{"Write"},
		ToolEnabled:               true,
		LatestMessageIsToolResult: true,
	}
	result := CompletionResult{AnswerText: "I will verify it next."}

	if !shouldForceToolContinuation(req, result) {
		t.Fatal("generic narration after a tool result should still trigger continuation recovery")
	}
}

func stringsJoinForTest(values ...string) string {
	out := ""
	for i, value := range values {
		if i > 0 {
			out += "\n"
		}
		out += value
	}
	return out
}
