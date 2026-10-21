<?php declare(strict_types = 0);
/**
 * Relatório RCA em página do frontend. Todo texto passa por CTag (escapado);
 * links só os de trace, remontados aqui a partir do traceid validado.
 *
 * Layout: resumo em linguagem simples sempre visível no topo (serviço,
 * severidade, confiança, causa provável, próximos passos) e, abaixo, um
 * <details> fechado por padrão com os detalhes técnicos (hipóteses com
 * códigos de evidência E1/E2..., tabela de evidências brutas e contagem de
 * mascaramento) — não precisa de JS, só abre quem quiser.
 *
 * @var CView $this
 * @var array $data
 */

$pagina = (new CHtmlPage())->setTitle(_('Relatório RCA do evento').' '.$data['eventid']);

if ($data['erro'] !== null) {
	$pagina->addItem(new CTag('p', true, $data['erro']))->show();
	return;
}

$lista = static function (array $itens): CTag {
	$ul = new CTag('ul', true);
	foreach ($itens as $i) {
		$ul->addItem(new CTag('li', true, (string) $i));
	}
	return $ul;
};

// Linha label/valor no mesmo padrão visual usado em outras páginas do Zabbix
// (ex.: cartão de serviço), já adaptado ao tema (claro/escuro) ativo.
$linha_info = static function (string $rotulo, $valor): array {
	return [
		(new CDiv($rotulo))->addClass(ZBX_STYLE_SERVICE_INFO_LABEL),
		(new CDiv($valor))->addClass(ZBX_STYLE_SERVICE_INFO_VALUE)
	];
};

// Evidência JSON crua -> mini-tabela chave/valor quando "dados" é um mapa
// (ex.: E1 evento do Zabbix, E2 trigger, resumo de spans); listas e textos
// soltos continuam como <pre>JSON</pre>, só mais fáceis de ler que o blob.
$dados_legiveis = static function ($dados) {
	if (!is_array($dados) || $dados === [] || array_keys($dados) === range(0, count($dados) - 1)) {
		return new CTag('pre', true, mb_substr(json_encode($dados, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT), 0, 4000));
	}
	$mini = (new CTableInfo())->setHeader([_('Campo'), _('Valor')]);
	foreach ($dados as $chave => $valor) {
		if (is_array($valor)) {
			$valor_html = new CTag('pre', true, mb_substr(json_encode($valor, JSON_UNESCAPED_UNICODE), 0, 500));
		}
		else {
			$valor_html = is_bool($valor) ? ($valor ? 'true' : 'false') : (string) ($valor ?? '-');
		}
		$mini->addRow([(string) $chave, $valor_html]);
	}
	return $mini;
};

$evidencia_por_tipo = static function (array $evidencias, string $tipo): ?array {
	foreach ($evidencias as $ev) {
		if (($ev['tipo'] ?? '') === $tipo) {
			return $ev;
		}
	}
	return null;
};

foreach (['pre' => _('Pré-análise'), 'profundo' => _('Análise aprofundada')] as $modo => $titulo) {
	$bloco = $data['relatorio'][$modo] ?? null;
	if (!is_array($bloco) || !is_array($bloco['relatorio'] ?? null)) {
		continue;
	}
	$rel = $bloco['relatorio'];
	$evidencias = $rel['evidencias'] ?? [];
	$pagina->addItem(new CTag('h2', true, $titulo.' — '.($rel['gerado_em'] ?? '').' ('.($bloco['status'] ?? '').')'));

	// ---- resumo simples, sempre visível -------------------------------------
	$linhas_resumo = [];
	$ev_evento = $evidencia_por_tipo($evidencias, 'zabbix.evento');
	$severidade = $ev_evento['dados']['severidade'] ?? null;

	$linhas_resumo[] = $linha_info(_('Serviço'), !empty($rel['servico']) ? $rel['servico'] : _('não identificado'));
	if ($severidade !== null && $severidade !== '') {
		$linhas_resumo[] = $linha_info(_('Severidade'),
			CSeverityHelper::makeSeverityCell((int) $severidade, null, false, true));
	}

	$corpo_resumo = [];
	$llm = ($rel['modo_ia'] ?? '') === 'llm';

	if (!$llm) {
		$corpo_resumo[] = new CTag('p', true, _('Modo somente evidências').': '.($rel['motivo_sem_llm'] ?? '').'. '
			._('Nenhum dado foi enviado ao Gemini.'));
		$corpo_resumo[] = new CTag('p', true, [
			new CTag('b', true, _('Resumo').': '), (string) ($rel['resumo_heuristico'] ?? '')
		]);
	}
	else {
		$a = $rel['analise'];
		$linhas_resumo[] = $linha_info(_('Confiança geral'), $a['confianca_geral'] ?? '-');
		$corpo_resumo[] = new CTag('p', true, [new CTag('b', true, _('Resumo').' ('.$rel['modelo'].'): '), $a['resumo']]);
		if ($a['hipoteses']) {
			$top = $a['hipoteses'][0];
			$corpo_resumo[] = new CTag('p', true, [
				new CTag('b', true, _('Causa provável').': '),
				$top['titulo'].' ('._('confiança').' '.$top['confianca'].')'
			]);
		}
		if ($a['proximos_passos']) {
			$corpo_resumo[] = new CTag('p', true, _('Próximos passos sugeridos (NÃO executados automaticamente):'));
			$corpo_resumo[] = $lista(array_map(
				static fn($s) => $s['acao'].' ('.$s['tipo'].', risco '.$s['risco'].')', $a['proximos_passos']
			));
		}
	}

	if (!empty($rel['nao_verificado'])) {
		$corpo_resumo[] = new CTag('p', true, _('Não verificado pela coleta:'));
		$corpo_resumo[] = $lista($rel['nao_verificado']);
	}

	$grid = (new CDiv())->addClass(ZBX_STYLE_SERVICE_INFO_GRID);
	foreach ($linhas_resumo as $linha) {
		$grid->addItem($linha);
	}
	$pagina->addItem((new CDiv(array_merge([$grid], $corpo_resumo)))->addClass(ZBX_STYLE_SERVICE_INFO));

	// ---- detalhes técnicos, recolhidos por padrão (sem JS) ------------------
	$detalhes = [];
	if ($llm) {
		$a = $rel['analise'];
		$detalhes[] = new CTag('p', true, _('Hipóteses detalhadas, em ordem de prioridade (E1, E2... = evidências da tabela abaixo):'));
		$detalhes[] = $lista(array_map(static fn($h) => $h['rank'].'. '.$h['titulo'].' — '.$h['categoria'].', '
			.$h['confianca'].'; evidências '.(implode(', ', $h['evidencias']) ?: '-').'; contra '
			.(implode(', ', $h['contra_evidencias']) ?: '-'), $a['hipoteses']));
		if ($a['nao_verificado']) {
			$detalhes[] = new CTag('p', true, _('Não verificado pelo modelo:'));
			$detalhes[] = $lista($a['nao_verificado']);
		}
	}

	$tabela = (new CTableInfo())->setHeader([_('ID'), _('Evidência'), _('Dados (mascarados)'), _('Traces')]);
	foreach ($evidencias as $ev) {
		$links = [];
		foreach ($ev['links'] ?? [] as $i => $url) {
			if (preg_match('/[?&]filter_traceid=([0-9a-fA-F]{16,64})$/', (string) $url, $m)) {
				$links[] = (new CLink(_('trace').' '.($i + 1),
					(new CUrl('zabbix.php'))
						->setArgument('action', 'apm.trace.list')
						->setArgument('filter_set', 1)
						->setArgument('filter_traceid', $m[1])
						->getUrl()
				))->setTarget('_blank');
				$links[] = ' ';
			}
		}
		$tabela->addRow([$ev['id'] ?? '', $ev['titulo'] ?? '', $dados_legiveis($ev['dados'] ?? null), $links]);
	}
	$detalhes[] = $tabela;
	$detalhes[] = new CTag('p', true, _('Mascaramento (ocorrências por tipo)').': '
		.json_encode($rel['mascaramento'] ?? [], JSON_UNESCAPED_UNICODE));

	$pagina->addItem(new CTag('details', true, array_merge(
		[new CTag('summary', true, _('Ver evidências técnicas brutas'))], $detalhes
	)));
}

$pagina->show();
