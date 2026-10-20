<?php declare(strict_types = 0);

namespace Modules\Investigador\Actions;

use CController,
	CControllerResponseData;

use Modules\Investigador\Widget;

/**
 * Pede a análise aprofundada (assíncrona). CSRF ativo; só para eventos que o
 * usuário logado enxerga. O investigador aplica cache, teto por hora e
 * orçamento diário.
 */
class WidgetAprofundar extends CController {

	protected function checkInput(): bool {
		$ret = $this->validateInput([
			'eventid' => 'required|db events.eventid',
			'pergunta' => 'string',
			'forcar' => 'in 0,1'
		]);

		if (!$ret) {
			$this->setResponse(new CControllerResponseData(['main_block' => json_encode(
				['erro' => _('Parâmetros inválidos.')], JSON_THROW_ON_ERROR)]));
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
			$rota = '/aprofundar/'.$eventid.($this->getInput('forcar', '0') === '1' ? '?forcar=1' : '');
			$r = Widget::chamarInvestigador('POST', $rota,
				['pergunta' => mb_substr($this->getInput('pergunta', ''), 0, 300)]
			);
			$saida = $r['dados'];
			$saida['http_status'] = $r['status'];
		}

		$this->setResponse(new CControllerResponseData(['main_block' => json_encode($saida, JSON_THROW_ON_ERROR)]));
	}
}
