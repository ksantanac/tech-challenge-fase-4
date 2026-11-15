package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// setupTelemetry configura o OpenTelemetry (traces + métricas) exportando via OTLP
// para o OTel Collector. Tudo é configurado pelas variáveis padrão do OTel:
//
//	OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES, OTEL_EXPORTER_OTLP_ENDPOINT.
//
// Com OTEL_SDK_DISABLED=true a telemetria fica desligada (no-op).
func setupTelemetry(ctx context.Context) (func(context.Context) error, error) {
	noop := func(context.Context) error { return nil }
	if strings.EqualFold(os.Getenv("OTEL_SDK_DISABLED"), "true") {
		log.Println("OpenTelemetry desabilitado (OTEL_SDK_DISABLED=true)")
		return noop, nil
	}

	res, err := resource.New(ctx,
		resource.WithFromEnv(), // service.name e atributos do Kubernetes (downward API)
		resource.WithTelemetrySDK(),
		resource.WithHost(),
	)
	if err != nil && res == nil {
		return noop, err
	}

	traceExporter, err := otlptracegrpc.New(ctx)
	if err != nil {
		return noop, err
	}
	tracerProvider := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(traceExporter),
		sdktrace.WithResource(res),
	)
	otel.SetTracerProvider(tracerProvider)

	// W3C Trace Context: é o que permite o trace "atravessar" os microsserviços.
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{}, propagation.Baggage{},
	))

	metricExporter, err := otlpmetricgrpc.New(ctx)
	if err != nil {
		return noop, err
	}
	meterProvider := sdkmetric.NewMeterProvider(
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(metricExporter, sdkmetric.WithInterval(15*time.Second))),
		sdkmetric.WithResource(res),
	)
	otel.SetMeterProvider(meterProvider)

	log.Println("OpenTelemetry inicializado (traces + métricas via OTLP)")
	return func(ctx context.Context) error {
		return errors.Join(tracerProvider.Shutdown(ctx), meterProvider.Shutdown(ctx))
	}, nil
}

// instrumentHandler envolve o roteador com o otelhttp (span + métricas HTTP por requisição)
// e com o log de acesso. O /health fica de fora para não poluir traces e métricas.
func instrumentHandler(next http.Handler, serviceName string) http.Handler {
	return otelhttp.NewHandler(accessLog(next), serviceName,
		otelhttp.WithFilter(func(r *http.Request) bool { return r.URL.Path != "/health" }),
		otelhttp.WithSpanNameFormatter(func(_ string, r *http.Request) string {
			return r.Method + " " + r.URL.Path
		}),
	)
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

// accessLog registra uma linha por requisição com o trace_id, o que permite ir do
// log (Loki) direto para o trace (APM).
func accessLog(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/health" {
			next.ServeHTTP(w, r)
			return
		}
		start := time.Now()
		traceID := trace.SpanContextFromContext(r.Context()).TraceID()
		// devolve o trace_id ao cliente: facilita achar a requisição no APM
		w.Header().Set("X-Trace-Id", traceID.String())
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)

		level := "INFO"
		if rec.status >= 500 {
			level = "ERROR"
		}
		log.Printf("level=%s method=%s path=%s status=%d duration_ms=%d trace_id=%s",
			level, r.Method, r.URL.Path, rec.status, time.Since(start).Milliseconds(), traceID)
	})
}
