<?php declare(strict_types = 0);

namespace Modules\Investigador\Actions;

use CController,
	CControllerResponseData;

use Modules\Investigador\Widget;

/**
 * Devolve o relatório (JSON) do investigador para um evento que o usuário
 * logado enxerga. CSRF ativo (token gerado na view do widget).
 */
class WidgetRelatorio extends CController {

	protected function checkInput(): bool {
		$ret = $this->validateInput(['eventid' => 'required|db events.eventid']);

		if (!$ret) {
			$this->setResponse(new CControllerResponseData(['main_block' => json_encode(
				['erro' => _('eventid inválido.')], JSON_THROW_ON_ERROR)]));
		}

		return $ret;
	}

	protected function checkPermissions(): bool {
		return $this->getUserType() >= USER_TYPE_ZABBIX_USER;
	}

	protected function doAction(): void {
		$eventid = (string) $this->getInput('eventid');

		if (!Widget::eventoVisivel($eventid)) {
			$saida = ['erro' => _('Evento não encontrado ou sem permissão.')];
		}
		else {
			$r = Widget::chamarInvestigador('GET', '/relatorio/'.$eventid);
			$saida = $r['dados'];
			$saida['http_status'] = $r['status'];
		}

		$this->setResponse(new CControllerResponseData(['main_block' => json_encode($saida, JSON_THROW_ON_ERROR)]));
	}
}
