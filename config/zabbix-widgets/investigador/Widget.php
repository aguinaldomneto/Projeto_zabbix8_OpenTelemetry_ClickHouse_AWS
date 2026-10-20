<?php declare(strict_types = 0);
/**
 * Widget "Investigador (RCA)" — laboratório Zabbix 8 + OpenTelemetry.
 *
 * Toda conversa com o serviço investigador acontece aqui, no servidor PHP:
 * o token (variável INVESTIGADOR_TOKEN do container zabbix-web) nunca vai
 * para o navegador. Antes de qualquer chamada, as actions conferem com a API
 * do Zabbix (com as permissões do usuário logado) que o evento é visível.
 */

namespace Modules\Investigador;

use API;
use Zabbix\Core\CWidget;

class Widget extends CWidget {

	public function getDefaultName(): string {
		return _('Investigador (RCA)');
	}

	public static function buscarProblemas(?array $groupids): array {
		$problems = API::Problem()->get([
			'output' => ['eventid', 'objectid', 'name', 'severity', 'clock'],
			'selectTags' => ['tag', 'value'],
			'groupids' => $groupids !== null ? getSubGroups($groupids) : null,
			'recent' => true,
			'sortfield' => ['eventid'],
			'sortorder' => ZBX_SORT_DOWN,
			'limit' => 15
		]) ?: [];

		$triggerids = array_unique(array_column($problems, 'objectid'));
		$hosts = $triggerids
			? API::Trigger()->get([
				'output' => [],
				'selectHosts' => ['name'],
				'triggerids' => $triggerids,
				'preservekeys' => true
			])
			: [];

		foreach ($problems as &$problem) {
			$t = $hosts[$problem['objectid']] ?? null;
			$problem['host'] = $t && $t['hosts'] ? $t['hosts'][0]['name'] : '';
			$problem['rca'] = '';
			foreach ($problem['tags'] as $tag) {
				if ($tag['tag'] === 'rca') {
					$problem['rca'] = $tag['value'];
				}
			}
		}
		unset($problem);

		return $problems;
	}

	/** O usuário logado enxerga este evento? (API roda com as permissões dele) */
	public static function eventoVisivel(string $eventid): bool {
		return (bool) API::Event()->get([
			'output' => ['eventid'],
			'eventids' => [$eventid],
			'limit' => 1
		]);
	}

	/**
	 * Chama o investigador. Devolve ['status' => int, 'dados' => array].
	 */
	public static function chamarInvestigador(string $metodo, string $rota, ?array $corpo = null): array {
		$base = getenv('INVESTIGADOR_URL') ?: 'http://investigador:8091';
		$token = getenv('INVESTIGADOR_TOKEN') ?: '';

		if ($token === '') {
			return ['status' => 0, 'dados' => ['erro' => _('INVESTIGADOR_TOKEN não configurado no zabbix-web.')]];
		}

		$ch = curl_init(rtrim($base, '/').$rota);
		$opcoes = [
			CURLOPT_CUSTOMREQUEST => $metodo,
			CURLOPT_HTTPHEADER => ['Content-Type: application/json', 'Authorization: Bearer '.$token],
			CURLOPT_RETURNTRANSFER => true,
			CURLOPT_CONNECTTIMEOUT => 3,
			CURLOPT_TIMEOUT => 10,
			CURLOPT_PROTOCOLS => CURLPROTO_HTTP | CURLPROTO_HTTPS,
			CURLOPT_FOLLOWLOCATION => false
		];
		if ($corpo !== null) {
			$opcoes[CURLOPT_POSTFIELDS] = json_encode($corpo, JSON_THROW_ON_ERROR);
		}
		curl_setopt_array($ch, $opcoes);

		$bruto = curl_exec($ch);
		$status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
		curl_close($ch);

		if ($bruto === false) {
			return ['status' => 0, 'dados' => ['erro' => _('Serviço investigador indisponível (make investigador-up).')]];
		}

		$dados = json_decode($bruto, true);

		return ['status' => $status, 'dados' => is_array($dados) ? $dados : ['erro' => _('Resposta inesperada do investigador.')]];
	}
}
