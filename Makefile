# Atalhos do laboratório. "make" sem argumentos mostra a lista.
.DEFAULT_GOAL := ajuda
COMPOSE := docker compose

.PHONY: ajuda env preflight up bootstrap verificar diagnostico ps logs down limpar digests mcp-up mcp-down tudo deploy vm-status vm-tunel vm-senha vm-parar vm-ligar vm-digests vm-liberar-ip hosts-up hosts-down ia-up ia-down investigador-up investigador-down investigador-config investigador-desligar investigador-remover investigador-teste investigador-teste-rapido ia-legado-up ia-legado-down local-up local-down gera-carga-de-estresse templates templates-status

ajuda: ## Mostra os comandos disponíveis
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  make %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

env: ## Cria o .env com senhas novas (não sobrescreve)
	@./scripts/gerar-env.sh

preflight: ## Confere memória, disco, Docker e portas antes de subir
	@./scripts/preflight.sh

up: ## Baixa as imagens, constrói o gerador e sobe tudo
	$(COMPOSE) pull --ignore-buildable
	$(COMPOSE) build
	$(COMPOSE) up -d
	@echo
	@echo "Subindo. A primeira vez leva alguns minutos (o server cria o banco)."
	@echo "Depois rode: make bootstrap && make verificar"

bootstrap: ## Troca a senha do Admin e cadastra o proxy com OpenTelemetry ligado
	$(COMPOSE) --profile tools run --rm bootstrap

tudo: ## Faz env, preflight, up, bootstrap e verificar em sequência (nesta máquina)
	@./scripts/subir-tudo.sh

verificar: ## Confere se a telemetria está chegando, etapa por etapa
	@./scripts/verificar.sh

diagnostico: ## Igual ao verificar, mas grava um arquivo com os logs para compartilhar
	@./scripts/verificar.sh --diagnostico

ps: ## Estado dos containers
	$(COMPOSE) ps -a

logs: ## Acompanha os logs (Ctrl+C sai). Ex.: make logs S=zabbix-proxy
	$(COMPOSE) logs -f --tail 100 $(S)

down: ## Para e remove os containers (os dados ficam nos volumes)
	$(COMPOSE) --profile mcp --profile tools --profile hosts --profile ia --profile ia-legado --profile investigador-teste down

limpar: ## APAGA os containers E os dados (pede confirmação)
	@printf 'Isso apaga o banco do Zabbix e toda a telemetria. Digite "apagar" para confirmar: '; \
	read resposta; [ "$$resposta" = "apagar" ] || { echo "Cancelado."; exit 1; }
	$(COMPOSE) --profile mcp --profile tools --profile hosts --profile ia --profile ia-legado --profile investigador-teste down -v

digests: ## Mostra o digest de cada imagem em uso (para fixar a versão)
	@docker image inspect --format '  {{index .RepoDigests 0}}' $$($(COMPOSE) config --images | grep -v ':local$$') 2>/dev/null \
	  || { echo "As imagens não estão nesta máquina. Do seu computador, use: make vm-digests"; exit 1; }

templates: ## Importa/atualiza os templates APM (OTel e APIs fictícias), hosts e dashboard (idempotente)
	@./scripts/importar-templates.sh

templates-status: ## Mostra o estado (ok/NOT SUPPORTED, último valor) dos itens e triggers dos templates
	@./scripts/importar-templates.sh --status

mcp-up: ## Sobe o servidor MCP do Zabbix (opcional, só em 127.0.0.1)
	$(COMPOSE) --profile mcp up -d zabbix-mcp-server

mcp-down: ## Para o servidor MCP
	$(COMPOSE) --profile mcp rm -sf zabbix-mcp-server

hosts-up: ## Sobe os 5 hosts simulados nesta VM (os outros 10 ficam no WSL local)
	$(COMPOSE) --profile hosts up -d

hosts-down: ## Para os 5 hosts simulados desta VM
	$(COMPOSE) --profile hosts rm -sf

ia-up: investigador-up ## (alias) Sobe o investigador de causa raiz

ia-down: investigador-down ## (alias) Para o investigador

investigador-up: ## Sobe o investigador (sem GEMINI_API_KEY = modo somente evidências)
	@./scripts/gerar-env.sh
	$(COMPOSE) build investigador
	$(COMPOSE) --profile ia up -d --no-deps investigador

investigador-down: ## Para o investigador (o cache/auditoria ficam no volume)
	$(COMPOSE) --profile ia rm -sf investigador

investigador-config: ## Cria/atualiza no Zabbix: usuário/papel/token, media type, action, módulo e dashboard (idempotente)
	@./scripts/configurar-investigador.sh

investigador-desligar: ## Desliga a action e o media type do investigador no Zabbix (não apaga nada)
	@./scripts/configurar-investigador.sh --desligar

investigador-remover: ## REMOVE do Zabbix tudo que o investigador-config criou
	@./scripts/configurar-investigador.sh --remover

investigador-teste: ## Testes do investigador com mock do Gemini (unitários + fluxo real com incidente simulado)
	@./scripts/investigador-teste.sh

investigador-teste-rapido: ## Só testes unitários e de integração sem disparar incidente
	@./scripts/investigador-teste.sh --rapido

ia-legado-up: ## Sobe o ia-assistente legado (rollback)
	$(COMPOSE) --profile ia-legado up -d --no-deps ia-assistente

ia-legado-down: ## Para o ia-assistente legado
	$(COMPOSE) --profile ia-legado rm -sf ia-assistente

# --- A partir do seu computador, para a VM da AWS ---------------------------
deploy: ## Acha a VM na AWS, copia esta pasta e sobe tudo nela
	@./scripts/deploy-aws.sh deploy

vm-status: ## Mostra a VM, o IP atual, a regra de SSH e se o acesso funciona
	@./scripts/deploy-aws.sh status

vm-tunel: ## Abre o túnel do frontend em http://localhost:8080
	@./scripts/deploy-aws.sh tunel

vm-digests: ## Mostra o digest das imagens que estão rodando na VM
	@./scripts/deploy-aws.sh digests

vm-senha: ## Mostra a senha do Admin do Zabbix que está na VM
	@./scripts/deploy-aws.sh senha

vm-parar: ## Para a VM para não gastar crédito (os dados ficam)
	@./scripts/deploy-aws.sh parar

vm-ligar: ## Liga a VM de novo (o IP muda)
	@./scripts/deploy-aws.sh ligar

vm-liberar-ip: ## Libera a porta 22 (SSH) da VM para o seu IP atual
	@./scripts/deploy-aws.sh liberar-ip

local-up: ## Sobe no WSL local: APIs fictícias, Redpanda, tráfego e 10 hosts
	@./scripts/subir-local.sh up

local-down: ## Para o que estiver rodando no WSL local
	@./scripts/subir-local.sh down

gera-carga-de-estresse: ## Dispara um pico de carga contra as APIs fictícias (no WSL local)
	@./scripts/subir-local.sh estresse
