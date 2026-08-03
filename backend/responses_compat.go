package main

import (
	"encoding/json"
	"io"
	"strings"
	"time"
)

const (
	responseStateLimit    = 256
	responseStateMaxBytes = 2 << 20
	responseStateTTL      = time.Hour
)

type storedResponseState struct {
	AuthToken string
	Items     []any
	CreatedAt time.Time
}

// expandResponsesBody restores the item history referenced by previous_response_id.
func (app *App) expandResponsesBody(body map[string]any, authToken string) (map[string]any, []any) {
	expanded := copyMap(body)
	items := []any{}
	previousID := stringValue(body, "previous_response_id", "")
	if previousID != "" {
		app.responsesMu.Lock()
		previous, ok := app.responses[previousID]
		app.responsesMu.Unlock()
		if ok && previous.AuthToken == authToken && time.Since(previous.CreatedAt) <= responseStateTTL {
			items = append(items, previous.Items...)
		}
	}
	items = append(items, responseInputItems(body["input"])...)
	if len(items) > 0 {
		expanded["input"] = items
	}
	return expanded, items
}

// rememberResponse retains the minimum item history needed by a follow-up Responses request.
func (app *App) rememberResponse(id, authToken string, inputItems []any, output []map[string]any) {
	items := append([]any(nil), inputItems...)
	for _, item := range output {
		items = append(items, item)
	}
	items = compactResponseStateItems(items, responseStateMaxBytes)
	raw, err := json.Marshal(items)
	if err != nil || len(raw) > responseStateMaxBytes {
		return
	}
	now := time.Now()
	app.responsesMu.Lock()
	defer app.responsesMu.Unlock()
	if app.responses == nil {
		app.responses = map[string]storedResponseState{}
	}
	for key, state := range app.responses {
		if now.Sub(state.CreatedAt) > responseStateTTL {
			delete(app.responses, key)
		}
	}
	if len(app.responses) >= responseStateLimit {
		oldestID := ""
		oldestAt := now
		for key, state := range app.responses {
			if oldestID == "" || state.CreatedAt.Before(oldestAt) {
				oldestID = key
				oldestAt = state.CreatedAt
			}
		}
		delete(app.responses, oldestID)
	}
	app.responses[id] = storedResponseState{AuthToken: authToken, Items: items, CreatedAt: now}
}

// compactResponseStateItems discards the oldest complete turns before persisting Responses state.
func compactResponseStateItems(items []any, maxBytes int) []any {
	compacted := append([]any(nil), items...)
	for len(compacted) > 1 {
		raw, err := json.Marshal(compacted)
		if err != nil || len(raw) <= maxBytes {
			return compacted
		}
		nextUser := -1
		for idx := 1; idx < len(compacted); idx++ {
			if responseItemRole(compacted[idx]) == "user" {
				nextUser = idx
				break
			}
		}
		if nextUser < 0 {
			break
		}
		compacted = append([]any(nil), compacted[nextUser:]...)
	}
	return compacted
}

// responseItemRole returns the conversational role used as a turn boundary.
func responseItemRole(item any) string {
	if _, ok := item.(string); ok {
		return "user"
	}
	value, _ := item.(map[string]any)
	return stringValue(value, "role", "")
}

// responseInputItems normalizes a Responses input value into reusable input items.
func responseInputItems(input any) []any {
	switch value := input.(type) {
	case nil:
		return nil
	case []any:
		return append([]any(nil), value...)
	default:
		return []any{value}
	}
}

// responsesStoreEnabled follows the Responses default of storing state unless explicitly disabled.
func responsesStoreEnabled(body map[string]any) bool {
	value, ok := body["store"]
	if !ok {
		return true
	}
	enabled, ok := value.(bool)
	return !ok || enabled
}

type responsesTextStream struct {
	w           io.Writer
	start       func()
	itemID      string
	started     bool
	text        strings.Builder
	outputIndex int
}

// newResponsesTextStream creates one Responses message item lifecycle.
func newResponsesTextStream(w io.Writer, start func()) *responsesTextStream {
	return &responsesTextStream{w: w, start: start, itemID: "msg_" + randomID()[:12]}
}

// WriteDelta starts the message item before writing its first text delta.
func (stream *responsesTextStream) WriteDelta(delta string) {
	if delta == "" {
		return
	}
	stream.ensureStarted()
	stream.text.WriteString(delta)
	writeSSEEvent(stream.w, "response.output_text.delta", map[string]any{
		"type": "response.output_text.delta", "item_id": stream.itemID, "output_index": stream.outputIndex,
		"content_index": 0, "delta": delta, "logprobs": []any{},
	})
}

// Finish closes the content part and message item and returns the completed item.
func (stream *responsesTextStream) Finish(text string) map[string]any {
	if stream.text.Len() == 0 && text != "" {
		stream.WriteDelta(text)
	} else {
		stream.ensureStarted()
	}
	if stream.text.Len() > 0 {
		text = stream.text.String()
	}
	part := map[string]any{"type": "output_text", "text": text, "annotations": []any{}, "logprobs": []any{}}
	item := map[string]any{
		"id": stream.itemID, "type": "message", "status": "completed", "role": "assistant",
		"content": []map[string]any{part},
	}
	writeSSEEvent(stream.w, "response.output_text.done", map[string]any{
		"type": "response.output_text.done", "item_id": stream.itemID, "output_index": stream.outputIndex,
		"content_index": 0, "text": text, "logprobs": []any{},
	})
	writeSSEEvent(stream.w, "response.content_part.done", map[string]any{
		"type": "response.content_part.done", "item_id": stream.itemID, "output_index": stream.outputIndex,
		"content_index": 0, "part": part,
	})
	writeSSEEvent(stream.w, "response.output_item.done", map[string]any{
		"type": "response.output_item.done", "output_index": stream.outputIndex, "item": item,
	})
	return item
}

// ensureStarted emits the item and content-part start events exactly once.
func (stream *responsesTextStream) ensureStarted() {
	if stream.started {
		return
	}
	stream.start()
	item := map[string]any{
		"id": stream.itemID, "type": "message", "status": "in_progress", "role": "assistant", "content": []any{},
	}
	writeSSEEvent(stream.w, "response.output_item.added", map[string]any{
		"type": "response.output_item.added", "output_index": stream.outputIndex, "item": item,
	})
	writeSSEEvent(stream.w, "response.content_part.added", map[string]any{
		"type": "response.content_part.added", "item_id": stream.itemID, "output_index": stream.outputIndex,
		"content_index": 0, "part": map[string]any{"type": "output_text", "text": "", "annotations": []any{}, "logprobs": []any{}},
	})
	stream.started = true
}

// writeResponsesToolItemEvents emits a complete function-call item lifecycle.
func writeResponsesToolItemEvents(w io.Writer, item map[string]any, outputIndex int) {
	pending := copyMap(item)
	pending["status"] = "in_progress"
	arguments, _ := item["arguments"].(string)
	pending["arguments"] = ""
	writeSSEEvent(w, "response.output_item.added", map[string]any{
		"type": "response.output_item.added", "output_index": outputIndex, "item": pending,
	})
	if arguments != "" {
		writeSSEEvent(w, "response.function_call_arguments.delta", map[string]any{
			"type": "response.function_call_arguments.delta", "item_id": item["id"], "output_index": outputIndex, "delta": arguments,
		})
	}
	writeSSEEvent(w, "response.function_call_arguments.done", map[string]any{
		"type": "response.function_call_arguments.done", "item_id": item["id"], "output_index": outputIndex, "arguments": arguments,
	})
	writeSSEEvent(w, "response.output_item.done", map[string]any{
		"type": "response.output_item.done", "output_index": outputIndex, "item": item,
	})
}
