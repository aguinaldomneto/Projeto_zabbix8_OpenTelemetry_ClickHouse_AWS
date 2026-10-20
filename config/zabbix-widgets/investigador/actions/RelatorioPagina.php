<?php declare(strict_types = 0);

namespace Modules\Investigador\Actions;

use CController,
	CControllerResponseData;

use Modules\Investigador\Widget;

/**
 * Página do relatório (zabbix.php?action=investigador.relatorio&eventid=N),
 * usada no link do comentário do evento e na entrada do menu do evento.
 * Mesma sessão/permissões do Zabbix; tudo renderizado com escape (CTag).
 */
class RelatorioPagina extends CController {

	protected function init(): void {
		// Página só de leitura via GET (não altera estado): sem CSRF, como as
		// demais páginas de visualização do Zabbix.
		$this->disableCsrfValidation();
	}

	protected function checkInput(): bool {
		return $this->validateInput(['eventid' => 'required|db events.eventid']);
	}

	protected function checkPermissions(): bool {
		return $this->getUserType() >= USER_TYPE_ZABBIX_USER;
	}

	protected function doAction(): void {
		$eventid = (string) $this->getInput('eventid');
		$dados = ['eventid' => $eventid, 'erro' => null, 'relatorio' => null];

		if (!Widget::eventoVisivel($eventid)) {
			$dados['erro'] = _('Evento não encontrado ou sem permissão.');
		}
		else {
			$r = Widget::chamarInvestigador('GET', '/relatorio/'.$eventid);
			if ($r['status'] !== 200) {
				$dados['erro'] = is_string($r['dados']['erro'] ?? null) ? $r['dados']['erro'] : _('Relatório indisponível.');
			}
			else {
				$dados['relatorio'] = $r['dados'];
			}
		}

		$resposta = new CControllerResponseData($dados);
		$resposta->setTitle(_('Relatório RCA'));
		$this->setResponse($resposta);
	}
}
