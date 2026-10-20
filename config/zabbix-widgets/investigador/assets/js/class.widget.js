/**
 * Widget "Investigador (RCA)". Tudo que vem do serviço/modelo é inserido com
 * textContent (nunca innerHTML); links de trace são remontados aqui a partir
 * do traceid validado (hex). As chamadas passam pelas actions PHP do módulo
 * (mesma sessão do Zabbix, CSRF ativo); o token do serviço fica no servidor.
 */
class CWidgetInvestigador extends CWidget {

	setContents(response) {
		super.setContents(response);

		const raiz = this._body.querySelector('.investigador-widget');

		if (!raiz) {
			return;
		}

		this._inv = {
			raiz,
			saida: raiz.querySelector('.investigador-relatorio'),
			csrf_relatorio: raiz.dataset.csrfRelatorio || '',
			csrf_aprofundar: raiz.dataset.csrfAprofundar || ''
		};

		raiz.addEventListener('click', (e) => {
			const botao = e.target.closest('button[data-eventid]');

			if (!botao) {
				return;
			}

			const eventid = botao.dataset.eventid;

			if (botao.classList.contains('js-inv-relatorio')) {
				this._invRelatorio(eventid);
			}
			else if (botao.classList.contains('js-inv-aprofundar')) {
				this._invAprofundar(eventid, botao);
			}
			else if (botao.classList.contains('js-inv-copiar')) {
				this._invCopiar(eventid, botao);
			}
		});
	}

	_invPost(acao, csrf, dados) {
		const url = new Curl('zabbix.php');
		url.setArgument('action', acao);

		const corpo = new URLSearchParams({...dados, [CSRF_TOKEN_NAME]: csrf});

		return fetch(url.getUrl(), {
			method: 'POST',
			headers: {'Content-Type': 'application/x-www-form-urlencoded'},
			body: corpo.toString()
		}).then((r) => r.json());
	}

	_invBuscar(eventid) {
		return this._invPost('widget.investigador.relatorio', this._inv.csrf_relatorio, {eventid});
	}

	_invMsg(texto) {
		this._inv.saida.replaceChildren();
		const p = document.createElement('p');
		p.textContent = texto;
		this._inv.saida.appendChild(p);
	}

	_invRelatorio(eventid) {
		this._invMsg(t('Carregando relatório...'));
		this._invBuscar(eventid)
			.then((d) => this._invRender(eventid, d))
			.catch(() => this._invMsg(t('Falha ao buscar o relatório.')));
	}

	_invAprofundar(eventid, botao) {
		botao.disabled = true;
		this._invMsg(t('Pedindo análise aprofundada...'));
		this._invPost('widget.investigador.aprofundar', this._inv.csrf_aprofundar, {eventid})
			.then((d) => {
				if (d.erro) {
					throw new Error(d.erro);
				}
				let tentativas = 0;
				const esperar = () => {
					this._invBuscar(eventid).then((r) => {
						const st = r.profundo ? r.profundo.status : '';
						if (st === 'pronto' || st === 'erro' || tentativas++ > 30) {
							botao.disabled = false;
							this._invRender(eventid, r);
						}
						else {
							this._invMsg(t('Aprofundando...') + ' (' + st + ')');
							setTimeout(esperar, 3000);
						}
					});
				};
				esperar();
			})
			.catch((e) => {
				botao.disabled = false;
				this._invMsg(String(e.message || t('Falha ao aprofundar.')));
			});
	}

	_invCopiar(eventid, botao) {
		this._invBuscar(eventid).then((d) => {
			const rel = (d.profundo && d.profundo.relatorio) || (d.pre && d.pre.relatorio);
			if (!rel || !rel.texto_copiavel) {
				this._invMsg(d.erro || t('Ainda não há resumo para este evento (use Aprofundar).'));
				return;
			}
			const texto = rel.texto_copiavel;
			const ok = () => this._invMsg(t('Resumo mascarado copiado. Cole em gemini.google.com/app (link abaixo da tabela).'));
			if (navigator.clipboard && window.isSecureContext) {
				navigator.clipboard.writeText(texto).then(ok, () => this._invCopiarFallback(texto, ok));
			}
			else {
				this._invCopiarFallback(texto, ok);
			}
		});
	}

	_invCopiarFallback(texto, ok) {
		const area = document.createElement('textarea');
		area.value = texto;
		area.setAttribute('readonly', '');
		area.style.position = 'fixed';
		area.style.opacity = '0';
		document.body.appendChild(area);
		area.select();
		try {
			document.execCommand('copy');
			ok();
		}
		catch (e) {
			this._invMsg(t('Não consegui copiar; selecione o texto manualmente.'));
		}
		area.remove();
	}

	_invEl(tag, texto) {
		const el = document.createElement(tag);
		if (texto !== undefined) {
			el.textContent = String(texto);
		}
		return el;
	}

	_invRender(eventid, d) {
		const s = this._inv.saida;
		s.replaceChildren();

		if (d.erro) {
			s.appendChild(this._invEl('p', d.erro + (d.modo_servico === 'somente-evidencias'
				? ' ' + t('(serviço em modo somente evidências)') : '')));
			return;
		}

		for (const [modo, titulo] of [['pre', t('Pré-análise')], ['profundo', t('Análise aprofundada')]]) {
			const bloco = d[modo];
			if (!bloco) {
				continue;
			}
			const rel = bloco.relatorio || {};
			s.appendChild(this._invEl('h4', titulo + ' — ' + (rel.gerado_em || '') + ' [' + bloco.status + ']'));

			if (!rel.evidencias) {
				s.appendChild(this._invEl('p', rel.erro || t('Em processamento...')));
				continue;
			}

			if (rel.modo_ia !== 'llm') {
				const aviso = this._invEl('p', t('Modo somente evidências') + ': ' + (rel.motivo_sem_llm || '')
					+ '. ' + t('Nenhum dado foi enviado ao Gemini.'));
				aviso.style.fontWeight = 'bold';
				s.appendChild(aviso);
				s.appendChild(this._invEl('p', rel.resumo_heuristico || ''));
			}
			else {
				const a = rel.analise;
				s.appendChild(this._invEl('p', t('Resumo') + ' (' + rel.modelo + ', ' + a.confianca_geral + '): ' + a.resumo));
				const ol = this._invEl('ol');
				for (const h of a.hipoteses) {
					ol.appendChild(this._invEl('li', h.titulo + ' — ' + h.categoria + ', ' + h.confianca + '; '
						+ (h.evidencias.join(', ') || '-')));
				}
				s.appendChild(ol);
				s.appendChild(this._invEl('p', t('Próximos passos sugeridos (NÃO executados automaticamente):')));
				const ul = this._invEl('ul');
				for (const p of a.proximos_passos) {
					ul.appendChild(this._invEl('li', '[' + p.tipo + ', risco ' + p.risco + '] ' + p.acao));
				}
				s.appendChild(ul);
			}

			for (const nv of (rel.nao_verificado || [])) {
				s.appendChild(this._invEl('p', t('Não verificado') + ': ' + nv));
			}

			const det = this._invEl('details');
			det.appendChild(this._invEl('summary', t('Evidências (mascaradas)') + ' — ' + rel.evidencias.length));
			for (const ev of rel.evidencias) {
				det.appendChild(this._invEl('b', ev.id + ' ' + ev.titulo));
				det.appendChild(this._invEl('pre', JSON.stringify(ev.dados, null, 1).slice(0, 3000)));
				for (const url of (ev.links || [])) {
					const m = /[?&]filter_traceid=([0-9a-fA-F]{16,64})$/.exec(url);
					if (m) {
						const link = new Curl('zabbix.php');
						link.setArgument('action', 'apm.trace.list');
						link.setArgument('filter_set', '1');
						link.setArgument('filter_traceid', m[1]);
						const a = this._invEl('a', 'trace ' + m[1].slice(0, 8));
						a.href = link.getUrl();
						a.target = '_blank';
						a.rel = 'noopener noreferrer';
						det.appendChild(a);
						det.appendChild(document.createTextNode(' '));
					}
				}
			}
			s.appendChild(det);
		}

		const pagina = new Curl('zabbix.php');
		pagina.setArgument('action', 'investigador.relatorio');
		pagina.setArgument('eventid', eventid);
		const link = this._invEl('a', t('Abrir relatório em página'));
		link.href = pagina.getUrl();
		link.target = '_blank';
		s.appendChild(link);
	}
}
