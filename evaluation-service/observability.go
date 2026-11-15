package main

import (
	"context"
	"log"
	"strconv"
	"strings"

	"github.com/go-redis/redis/v8"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"
)

const instrumentationName = "evaluation-service"

var tracer = otel.Tracer(instrumentationName)

// Métricas de negócio (aparecem no Prometheus/Grafana e no APM).
var (
	evaluationsCounter metric.Int64Counter
	cacheCounter       metric.Int64Counter
)

func initMetrics() {
	meter := otel.Meter(instrumentationName)
	var err error
	evaluationsCounter, err = meter.Int64Counter("togglemaster.evaluations",
		metric.WithDescription("Total de avaliações de feature flags"))
	if err != nil {
		log.Printf("Aviso: métrica togglemaster.evaluations: %v", err)
	}
	cacheCounter, err = meter.Int64Counter("togglemaster.cache.requests",
		metric.WithDescription("Consultas ao cache Redis (hit/miss)"))
	if err != nil {
		log.Printf("Aviso: métrica togglemaster.cache.requests: %v", err)
	}
}

func recordEvaluation(ctx context.Context, flagName string, result bool) {
	if evaluationsCounter != nil {
		evaluationsCounter.Add(ctx, 1, metric.WithAttributes(
			attribute.String("flag_name", flagName),
			attribute.Bool("result", result),
		))
	}
}

func recordCache(ctx context.Context, result string) {
	if cacheCounter != nil {
		cacheCounter.Add(ctx, 1, metric.WithAttributes(attribute.String("result", result)))
	}
}

// redisTracingHook cria um span (client) para cada comando enviado ao Redis,
// seguindo as convenções semânticas do OpenTelemetry (db.system=redis).
type redisTracingHook struct {
	host string
	port int
}

func newRedisTracingHook(addr string) *redisTracingHook {
	host, portStr, _ := strings.Cut(addr, ":")
	port, _ := strconv.Atoi(portStr)
	return &redisTracingHook{host: host, port: port}
}

func (h *redisTracingHook) attrs(op string) []attribute.KeyValue {
	return []attribute.KeyValue{
		attribute.String("db.system", "redis"),
		attribute.String("db.operation", op),
		attribute.String("server.address", h.host),
		attribute.Int("server.port", h.port),
	}
}

func (h *redisTracingHook) BeforeProcess(ctx context.Context, cmd redis.Cmder) (context.Context, error) {
	ctx, _ = tracer.Start(ctx, "redis "+strings.ToUpper(cmd.Name()),
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(h.attrs(strings.ToUpper(cmd.Name()))...))
	return ctx, nil
}

func (h *redisTracingHook) AfterProcess(ctx context.Context, cmd redis.Cmder) error {
	span := trace.SpanFromContext(ctx)
	// redis.Nil (chave não existe = cache miss) não é erro
	if err := cmd.Err(); err != nil && err != redis.Nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
	}
	span.End()
	return nil
}

func (h *redisTracingHook) BeforeProcessPipeline(ctx context.Context, cmds []redis.Cmder) (context.Context, error) {
	ctx, _ = tracer.Start(ctx, "redis PIPELINE",
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(h.attrs("PIPELINE")...))
	return ctx, nil
}

func (h *redisTracingHook) AfterProcessPipeline(ctx context.Context, _ []redis.Cmder) error {
	trace.SpanFromContext(ctx).End()
	return nil
}
