<?php declare(strict_types = 0);
/**
 * Relatório RCA em página do frontend. Todo texto passa por CTag (escapado);
 * links só os de trace, remontados aqui a partir do traceid validado.
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

foreach (['pre' => _('Pré-análise'), 'profundo' => _('Análise aprofundada')] as $modo => $titulo) {
	$bloco = $data['relatorio'][$modo] ?? null;
	if (!is_array($bloco) || !is_array($bloco['relatorio'] ?? null)) {
		continue;
	}
	$rel = $bloco['relatorio'];
	$pagina->addItem(new CTag('h2', true, $titulo.' — '.($rel['gerado_em'] ?? '').' ('.($bloco['status'] ?? '').')'));

	if (($rel['modo_ia'] ?? '') !== 'llm') {
		$pagina->addItem(new CTag('p', true, _('Modo somente evidências').': '.($rel['motivo_sem_llm'] ?? '').'. '
			._('Nenhum dado foi enviado ao Gemini.')));
		$pagina->addItem(new CTag('p', true, (string) ($rel['resumo_heuristico'] ?? '')));
	}
	else {
		$a = $rel['analise'];
		$pagina->addItem(new CTag('p', true, _('Resumo').' ('.$rel['modelo'].'): '.$a['resumo']));
		$pagina->addItem($lista(array_map(static fn($h) => $h['rank'].'. '.$h['titulo'].' — '.$h['categoria'].', '
			.$h['confianca'].'; '.implode(', ', $h['evidencias']), $a['hipoteses'])));
		$pagina->addItem(new CTag('p', true, _('Próximos passos sugeridos (NÃO executados automaticamente):')));
		$pagina->addItem($lista(array_map(static fn($s) => '['.$s['tipo'].', risco '.$s['risco'].'] '.$s['acao'],
			$a['proximos_passos'])));
		if ($a['nao_verificado']) {
			$pagina->addItem(new CTag('p', true, _('Não verificado pelo modelo:')));
			$pagina->addItem($lista($a['nao_verificado']));
		}
	}

	if (!empty($rel['nao_verificado'])) {
		$pagina->addItem(new CTag('p', true, _('Não verificado pela coleta:')));
		$pagina->addItem($lista($rel['nao_verificado']));
	}

	$tabela = (new CTableInfo())->setHeader([_('ID'), _('Evidência'), _('Dados (mascarados)'), _('Traces')]);
	foreach ($rel['evidencias'] ?? [] as $ev) {
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
		$tabela->addRow([
			$ev['id'] ?? '',
			$ev['titulo'] ?? '',
			new CTag('pre', true, mb_substr(json_encode($ev['dados'] ?? null, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT), 0, 4000)),
			$links
		]);
	}
	$pagina->addItem($tabela);
	$pagina->addItem(new CTag('p', true, _('Mascaramento (ocorrências por tipo)').': '
		.json_encode($rel['mascaramento'] ?? [], JSON_UNESCAPED_UNICODE)));
}

$pagina->show();
