<?php declare(strict_types = 0);

namespace Modules\Insights\Actions;

use CController,
	CControllerResponseData;

use Modules\Insights\Widget;

/**
 * Ação do botão "Analisar com IA": busca os problemas recentes do(s) grupo(s)
 * configurado(s) no widget e pede um resumo ao serviço de IA (investigador,
 * ou o ia-assistente legado). A chave do Gemini nunca passa por aqui nem pelo
 * navegador.
 *
 * Proteção CSRF LIGADA (antes era desligada): o token é gerado na view
 * (CCsrfTokenHelper::get('widget.insights.analyze')) e enviado pelo JS em
 * _csrf_token. A chamada ao serviço leva "Authorization: Bearer" com o token
 * compartilhado (variável IA_ASSISTENTE_TOKEN do container zabbix-web).
 */
class WidgetAnalyze extends CController {

	protected function checkInput(): bool {
		$fields = [
			'groupids' => 'array_id',
			'pergunta' => 'string'
		];

		$ret = $this->validateInput($fields);

		if (!$ret) {
			$this->setResponse(new CControllerResponseData(['main_block' => json_encode([
				'erro' => _('Parâmetros inválidos.')
			], JSON_THROW_ON_ERROR)]));
		}

		return $ret;
	}

	protected function checkPermissions(): bool {
		return $this->getUserType() >= USER_TYPE_ZABBIX_USER;
	}

	protected function doAction(): void {
		$groupids = $this->getInput('groupids', []);
		$pergunta = mb_substr($this->getInput('pergunta', ''), 0, 300);

		// Problem.get roda com as permissões do usuário logado.
		$problems = Widget::buscarProblemas($groupids ?: null);

		$contexto = array_map(static function (array $problem): array {
			return [
				'eventid' => $problem['eventid'],
				'host' => $problem['host'],
				'problema' => $problem['name'],
				'severidade' => $problem['severity'],
				'desde' => date('Y-m-d H:i:s', (int) $problem['clock'])
			];
		}, $problems);

		$this->setResponse(new CControllerResponseData([
			'main_block' => json_encode($this->chamarServico($contexto, $pergunta), JSON_THROW_ON_ERROR)
		]));
	}

	private function chamarServico(array $contexto, string $pergunta): array {
		$base = getenv('IA_ASSISTENTE_URL') ?: 'http://investigador:8091';
		$token = getenv('IA_ASSISTENTE_TOKEN') ?: '';

		if ($token === '') {
			return ['erro' => _('Serviço de IA sem token configurado (IA_ASSISTENTE_TOKEN).')];
		}

		$ch = curl_init(rtrim($base, '/').'/analisar-painel');
		curl_setopt_array($ch, [
			CURLOPT_POST => true,
			CURLOPT_POSTFIELDS => json_encode(['contexto' => $contexto, 'pergunta' => $pergunta], JSON_THROW_ON_ERROR),
			CURLOPT_HTTPHEADER => ['Content-Type: application/json', 'Authorization: Bearer '.$token],
			CURLOPT_RETURNTRANSFER => true,
			CURLOPT_CONNECTTIMEOUT => 3,
			CURLOPT_TIMEOUT => 40,
			CURLOPT_PROTOCOLS => CURLPROTO_HTTP | CURLPROTO_HTTPS,
			CURLOPT_FOLLOWLOCATION => false
		]);

		$bruto = curl_exec($ch);
		$status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
		curl_close($ch);

		if ($bruto === false) {
			return ['erro' => _('Serviço de IA indisponível.')];
		}

		$dados = json_decode($bruto, true);
		if (!is_array($dados)) {
			return ['erro' => _('Resposta inesperada do serviço de IA.')];
		}

		if ($status >= 400) {
			return ['erro' => is_string($dados['erro'] ?? null) ? mb_substr($dados['erro'], 0, 200) : _('Falha no serviço de IA.')];
		}

		return [
			'resposta' => is_string($dados['resposta'] ?? null) ? $dados['resposta'] : '',
			'modo' => is_string($dados['modo'] ?? null) ? $dados['modo'] : ''
		];
	}
}
