-- Regras de segmentação (rollout por porcentagem de usuários).
INSERT INTO targeting_rules (flag_name, is_enabled, rules) VALUES
  ('novo-checkout', true, '{"type": "PERCENTAGE", "value": 50}'),
  ('pagamento-pix', true, '{"type": "PERCENTAGE", "value": 20}')
ON CONFLICT (flag_name) DO NOTHING;
