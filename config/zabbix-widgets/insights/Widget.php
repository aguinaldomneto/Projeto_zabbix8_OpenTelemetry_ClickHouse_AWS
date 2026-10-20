<?php declare(strict_types = 0);
/**
 * Widget "Insights (IA)": resume os problemas do painel com a API do
 * Gemini. Laboratório Zabbix 8 + OpenTelemetry.
 */

namespace Modules\Insights;

use API;
use Zabbix\Core\CWidget;

class Widget extends CWidget {

	public function getDefaultName(): string {
		return _('Insights (IA)');
	}

	/**
	 * Problemas recentes (opcionalmente filtrados por grupo de hosts), já
	 * com o nome do host resolvido via Trigger.get (Problem.get não tem
	 * selectHosts). Usado pela view e pela action "Analisar com IA".
	 */
	public static function buscarProblemas(?array $groupids): array {
		$problems = API::Problem()->get([
			'output' => ['eventid', 'objectid', 'name', 'severity', 'clock'],
			'groupids' => $groupids !== null ? getSubGroups($groupids) : null,
			'recent' => true,
			'limit' => 20
		]) ?: [];

		$triggerids = array_unique(array_column($problems, 'objectid'));
		$hosts_por_trigger = $triggerids
			? API::Trigger()->get([
				'output' => [],
				'selectHosts' => ['name'],
				'triggerids' => $triggerids,
				'preservekeys' => true
			])
			: [];

		foreach ($problems as &$problem) {
			$trigger = $hosts_por_trigger[$problem['objectid']] ?? null;
			$problem['host'] = $trigger && $trigger['hosts'] ? $trigger['hosts'][0]['name'] : '';
		}
		unset($problem);

		return $problems;
	}
}
