<?php declare(strict_types = 0);

namespace Modules\Investigador\Actions;

use CButton,
	CCol,
	CControllerDashboardWidgetView,
	CControllerResponseData,
	CCsrfTokenHelper,
	CDiv,
	CLink,
	CSpan,
	CTableInfo;

use Modules\Investigador\Widget;

class WidgetView extends CControllerDashboardWidgetView {

	protected function doAction(): void {
		$groupids = $this->fields_values['groupids'] ?: null;
		$problems = Widget::buscarProblemas($groupids);

		$table = (new CTableInfo())
			->setHeader([_('Hora'), _('Host'), _('Problema'), _('Sev.'), _('RCA'), _('Ações')]);

		foreach ($problems as $p) {
			$acoes = [
				(new CButton(null, _('Relatório')))
					->addClass('js-inv-relatorio')
					->setAttribute('data-eventid', $p['eventid']),
				' ',
				(new CButton(null, _('Aprofundar')))
					->addClass('js-inv-aprofundar')
					->setAttribute('data-eventid', $p['eventid']),
				' ',
				(new CButton(null, _('Copiar resumo para o Gemini')))
					->addClass('js-inv-copiar')
					->setAttribute('data-eventid', $p['eventid'])
			];

			$table->addRow([
				new CCol(date('d/m H:i', (int) $p['clock'])),
				new CCol($p['host']),
				new CCol($p['name']),
				new CCol($p['severity']),
				new CCol($p['rca'] !== '' ? $p['rca'] : '-'),
				new CCol($acoes)
			]);
		}

		if (!$problems) {
			$table->setNoDataMessage(_('Nenhum problema recente.'));
		}

		$raiz = (new CDiv([
			$table,
			(new CDiv([
				new CSpan(_('Resumo copiado? Cole em ')),
				(new CLink('gemini.google.com/app', 'https://gemini.google.com/app'))
					->setTarget('_blank')
					->setAttribute('rel', 'noopener noreferrer'),
				new CSpan(_(' (o texto já sai mascarado; prefira o botão Aprofundar quando a chave estiver configurada).'))
			]))->addClass('investigador-dica'),
			(new CDiv())->addClass('investigador-relatorio')
		]))
			->addClass('investigador-widget')
			->setAttribute('data-csrf-relatorio', CCsrfTokenHelper::get('widget.investigador.relatorio'))
			->setAttribute('data-csrf-aprofundar', CCsrfTokenHelper::get('widget.investigador.aprofundar'));

		$this->setResponse(new CControllerResponseData([
			'name' => $this->getInput('name', $this->widget->getDefaultName()),
			'raiz' => $raiz,
			'user' => [
				'debug_mode' => $this->getDebugMode()
			]
		]));
	}
}
