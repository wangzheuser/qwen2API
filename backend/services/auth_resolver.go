package services

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"strings"
)

type AuthCredentials struct {
	Token   string
	Cookies string
	Email   string
}

func ResolveBearerToken(r *http.Request) string {
	if r == nil {
		return ""
	}
	auth := strings.TrimSpace(r.Header.Get("Authorization"))
	if strings.HasPrefix(strings.ToLower(auth), "bearer ") {
		return strings.TrimSpace(auth[7:])
	}
	if token := strings.TrimSpace(r.URL.Query().Get("api_key")); token != "" {
		return token
	}
	return strings.TrimSpace(r.Header.Get("X-API-Key"))
}

func ResolveQwenCredentials(headers http.Header) AuthCredentials {
	return AuthCredentials{
		Token:   strings.TrimSpace(headers.Get("X-Qwen-Token")),
		Cookies: strings.TrimSpace(headers.Get("X-Qwen-Cookies")),
		Email:   strings.TrimSpace(headers.Get("X-Qwen-Account")),
	}
}

// TokenExpiry 解析 JWT 的 exp（秒级时间戳）；无法解析时返回 0。
// 仅读取 payload 段，不验签——只需要判断过期时间。
func TokenExpiry(token string) float64 {
	if token == "" {
		return 0
	}
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return 0
	}
	// JWT 使用 base64url，且通常省略 padding，用 RawURLEncoding 解码。
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return 0
	}
	var claims struct {
		Exp float64 `json:"exp"`
	}
	if err := json.Unmarshal(raw, &claims); err != nil {
		return 0
	}
	return claims.Exp
}
