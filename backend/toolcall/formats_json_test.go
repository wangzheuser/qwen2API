package toolcall

import (
	"strings"
	"testing"
	"time"
)

// TestForEachJSONFragmentFindsEmbeddedAndLooseJSON covers preserved parser formats.
func TestForEachJSONFragmentFindsEmbeddedAndLooseJSON(t *testing.T) {
	text := `prefix {"name":"weather","arguments":{"city":"Paris"}} middle {name:"weather","arguments":{"city":"Tokyo"}} suffix`
	var values []any

	ForEachJSONFragment(text, func(value any) {
		values = append(values, value)
	})

	if len(values) != 2 {
		t.Fatalf("got %d JSON values, want 2", len(values))
	}
}

// TestForEachJSONFragmentLargeIncompleteInputCompletesQuickly guards against nested rescans.
func TestForEachJSONFragmentLargeIncompleteInputCompletesQuickly(t *testing.T) {
	text := `{"name":"weather","arguments":` + strings.Repeat("{", 128<<10)
	done := make(chan struct{})

	go func() {
		ForEachJSONFragment(text, func(any) {})
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("large incomplete JSON triggered superlinear scanning")
	}
}

// BenchmarkForEachJSONFragmentIncomplete tracks the malformed-stream allocation ceiling.
func BenchmarkForEachJSONFragmentIncomplete(b *testing.B) {
	text := `{"name":"weather","arguments":` + strings.Repeat("{", 128<<10)
	b.ReportAllocs()
	for b.Loop() {
		ForEachJSONFragment(text, func(any) {})
	}
}
