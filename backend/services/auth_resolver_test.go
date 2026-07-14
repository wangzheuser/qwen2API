package services

import (
	"encoding/base64"
	"encoding/json"
	"testing"
)

func makeJWT(payload map[string]any) string {
	enc := func(v any) string {
		b, _ := json.Marshal(v)
		return base64.RawURLEncoding.EncodeToString(b)
	}
	return enc(map[string]any{"alg": "HS256"}) + "." + enc(payload) + ".sig"
}

func TestTokenExpiryValid(t *testing.T) {
	tok := makeJWT(map[string]any{"id": "u1", "exp": 1893456000})
	if got := TokenExpiry(tok); got != 1893456000 {
		t.Fatalf("want 1893456000, got %v", got)
	}
}

func TestTokenExpiryEmpty(t *testing.T) {
	if got := TokenExpiry(""); got != 0 {
		t.Fatalf("want 0, got %v", got)
	}
}

func TestTokenExpiryMalformed(t *testing.T) {
	if got := TokenExpiry("not-a-jwt"); got != 0 {
		t.Fatalf("want 0, got %v", got)
	}
}

func TestTokenExpiryNoExp(t *testing.T) {
	tok := makeJWT(map[string]any{"id": "u1"})
	if got := TokenExpiry(tok); got != 0 {
		t.Fatalf("want 0, got %v", got)
	}
}
