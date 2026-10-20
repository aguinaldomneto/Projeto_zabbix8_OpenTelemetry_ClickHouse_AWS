<?php declare(strict_types = 0);

namespace Modules\Insights\Actions;

use CButton,
	CControllerDashboardWidgetView,
	CControllerResponseData,
	CCsrfTokenHelper,
	CDiv,
	CTableInfo,
	CCol;

use Modules\Insights\Widget;

class WidgetView extends CControllerDashboardWidgetView {

	protected function doAction(): void {
		$groupids = $this->fields_values['groupids'] ?: null;
		$problems = Widget::buscarProblemas($groupids);

		$table = (new CTableInfo())
			->setHeader([_('Hora'), _('Host'), _('Problema')]);

		foreach ($problems as $problem) {
			$table->addRow([
				new CCol(date('H:i:s', (int) $problem['clock'])),
				new CCol($problem['host']),
				new CCol($problem['name'])
			]);
		}

		if (!$problems) {
			$table->setNoDataMessage(_('Nenhum problema recente.'));
		}

		// Token CSRF da action widget.insights.analyze (proteção religada).
		$botao = (new CButton(null, _('Analisar com IA')))
			->addClass('js-insights-analyze')
			->setAttribute('data-csrf', CCsrfTokenHelper::get('widget.insights.analyze'));

		$resultado = (new CDiv())->addClass('insights-resultado');

		$this->setResponse(new CControllerResponseData([
			'name' => $this->getInput('name', $this->widget->getDefaultName()),
			'table' => $table,
			'botao' => $botao,
			'resultado' => $resultado,
			'user' => [
				'debug_mode' => $this->getDebugMode()
			]
		]));
	}
}
