package main

import (
	"encoding/base64"
	"encoding/json"
	"testing"
	"time"
)

func makeExpToken(exp float64) string {
	enc := func(v any) string {
		b, _ := json.Marshal(v)
		return base64.RawURLEncoding.EncodeToString(b)
	}
	return "h." + enc(map[string]any{"exp": exp}) + ".s"
}

func refreshTestEmails(accs []Account) []string {
	out := make([]string, 0, len(accs))
	for _, a := range accs {
		out = append(out, a.Email)
	}
	return out
}

func TestDueAccountsForRefresh(t *testing.T) {
	now := float64(time.Now().Unix())
	ahead := float64(259200)
	accounts := []Account{
		{Email: "expiring", Password: "p", Token: makeExpToken(now + 3600), Source: "file"}, // 应刷
		{Email: "fresh", Password: "p", Token: makeExpToken(now + 999999), Source: "file"},  // 远期，不刷
		{Email: "nopwd", Password: "", Token: makeExpToken(now + 3600), Source: "file"},     // 无密码
		{Email: "env", Password: "p", Token: makeExpToken(now + 3600), Source: "env"},       // env
		{Email: "expired", Password: "p", Token: makeExpToken(now - 100), Source: "file"},   // 已过期
		{Email: "garbage", Password: "p", Token: "not-a-jwt", Source: "file"},               // 畸形
	}
	due := dueAccountsForRefresh(accounts, now, ahead)
	if got := refreshTestEmails(due); len(got) != 3 || got[0] != "expiring" || got[1] != "expired" || got[2] != "garbage" {
		t.Fatalf("want [expiring expired garbage], got %v", got)
	}
}
