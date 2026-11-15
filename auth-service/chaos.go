package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"sync/atomic"
)

// faultMode simula uma falha "silenciosa" de estado em memória (ex.: pool de conexões
// corrompido, memory leak). Enquanto estiver ligada, o /validate responde HTTP 500.
// Como o estado vive só na memória do pod, um `rollout restart` (self-healing) cura o
// serviço — exatamente o cenário que o alerta + automação devem resolver sozinhos.
var faultMode atomic.Bool

type chaosRequest struct {
	Enabled bool `json:"enabled"`
}

func faultInjected() bool {
	return faultMode.Load()
}

// chaosHandler: GET consulta o estado; POST {"enabled": true|false} liga/desliga a falha
// neste pod. Protegido pela MASTER_KEY.
func (a *App) chaosHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	hostname, _ := os.Hostname()

	switch r.Method {
	case http.MethodGet:
	case http.MethodPost:
		var req chaosRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, `{"error": "corpo inválido, use {\"enabled\": true}"}`, http.StatusBadRequest)
			return
		}
		faultMode.Store(req.Enabled)
		log.Printf("level=WARN msg=\"chaos alterado\" pod=%s fault_injected=%t", hostname, req.Enabled)
	default:
		http.Error(w, "Método não permitido", http.StatusMethodNotAllowed)
		return
	}

	_ = json.NewEncoder(w).Encode(map[string]any{
		"pod":            hostname,
		"fault_injected": faultMode.Load(),
	})
}
