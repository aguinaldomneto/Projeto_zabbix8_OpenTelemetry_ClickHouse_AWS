<?php declare(strict_types = 0);

namespace Modules\Insights\Actions;

use CController,
	CControllerResponseData;

use Modules\Insights\Widget;

/**
 * Ação do botão "Analisar com IA": busca os problemas recentes do(s) grupo(s)
 * configurado(s) no widget e pede ao serviço ia-assistente (que fala com a
 * API do Gemini) um resumo. A chave do Gemini nunca passa por aqui nem pelo
 * navegador — fica só no ia-assistente.
 */
class WidgetAnalyze extends CController {

	protected function init(): void {
		$this->disableCsrfValidation();
	}

	protected function checkInput(): bool {
		$fields = [
			'groupids' => 'array_id',
			'pergunta' => 'string'
		];

		$ret = $this->validateInput($fields);

		if (!$ret) {
			$this->setResponse(new CControllerResponseData(['main_block' => json_encode([
				'erro' => implode(' ', array_column(get_and_clear_messages(), 'message'))
			], JSON_THROW_ON_ERROR)]));
		}

		return $ret;
	}

	protected function checkPermissions(): bool {
		return $this->getUserType() >= USER_TYPE_ZABBIX_USER;
	}

	protected function doAction(): void {
		$groupids = $this->getInput('groupids', []);
		$pergunta = $this->getInput('pergunta', '');

		$problems = Widget::buscarProblemas($groupids ?: null);

		$contexto = array_map(static function (array $problem): array {
			return [
				'host' => $problem['host'],
				'problema' => $problem['name'],
				'severidade' => $problem['severity'],
				'desde' => date('Y-m-d H:i:s', (int) $problem['clock'])
			];
		}, $problems);

		$resposta = $this->chamarIaAssistente($contexto, $pergunta);

		$this->setResponse(new CControllerResponseData([
			'main_block' => json_encode($resposta, JSON_THROW_ON_ERROR)
		]));
	}

	private function chamarIaAssistente(array $contexto, string $pergunta): array {
		$url = rtrim(getenv('IA_ASSISTENTE_URL') ?: 'http://ia-assistente:8090', '/').'/analisar-painel';
		$corpo = json_encode(['contexto' => $contexto, 'pergunta' => $pergunta], JSON_THROW_ON_ERROR);

		$ch = curl_init($url);
		curl_setopt_array($ch, [
			CURLOPT_POST => true,
			CURLOPT_POSTFIELDS => $corpo,
			CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
			CURLOPT_RETURNTRANSFER => true,
			CURLOPT_TIMEOUT => 35
		]);

		$bruto = curl_exec($ch);
		$erro_curl = curl_error($ch);
		curl_close($ch);

		if ($bruto === false) {
			return ['erro' => _('Não consegui falar com o ia-assistente: ').$erro_curl];
		}

		$dados = json_decode($bruto, true);
		if (!is_array($dados)) {
			return ['erro' => _('Resposta inesperada do ia-assistente.')];
		}

		return $dados;
	}
}
