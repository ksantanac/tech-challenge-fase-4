-- Flags de exemplo usadas pelo gerador de tráfego e na demonstração.
INSERT INTO flags (name, description, is_enabled) VALUES
  ('novo-checkout',       'Novo fluxo de checkout (rollout de 50%)', true),
  ('dark-mode',           'Tema escuro para todos os usuários',      true),
  ('pagamento-pix',       'Pagamento via PIX (rollout de 20%)',      true),
  ('banner-black-friday', 'Banner da Black Friday (desligado)',      false)
ON CONFLICT (name) DO NOTHING;
