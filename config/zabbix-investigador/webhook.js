// Media type webhook "Investigador IA (RCA)" - Zabbix 8.
// Envia o evento ao serviço investigador e devolve tags (Process tags ligado).
// É ASSÍNCRONO do ponto de vista do Zabbix: o investigador só enfileira e
// responde em < 1 s; a análise e o comentário no evento (event.acknowledge
// action=4) chegam depois. Nenhum dado do evento vai para fora da rede
// interna aqui; o mascaramento acontece no investigador.
var p = JSON.parse(value);

if (typeof p.url !== 'string' || p.url.indexOf('http://investigador:') !== 0 && p.url.indexOf('https://') !== 0) {
    throw 'URL do investigador inválida (macro {$INVESTIGADOR.URL})';
}
if (!p.token || p.token.indexOf('{$') === 0) {
    throw 'token do investigador ausente (macro {$INVESTIGADOR.TOKEN})';
}

var corpo = {
    eventid: p.eventid,
    valor: p.valor,
    servico: p.servico,
    host: p.host,
    severidade: p.severidade
};

var req = new HttpRequest();
req.addHeader('Content-Type: application/json');
req.addHeader('Authorization: Bearer ' + p.token);

var resposta = req.post(p.url.replace(/\/+$/, '') + '/evento', JSON.stringify(corpo));
var status = req.getStatus();

if (status === 401) {
    throw 'investigador recusou o token (HTTP 401)';
}
if (status !== 200 && status !== 202) {
    throw 'investigador respondeu HTTP ' + status;
}

var dados = {};
try {
    dados = JSON.parse(resposta);
} catch (e) {
    dados = {};
}

var tags = {};
if (dados.tags && typeof dados.tags === 'object') {
    for (var k in dados.tags) {
        if (/^rca[._a-z]*$/.test(k)) {
            tags[k] = String(dados.tags[k]).substring(0, 100);
        }
    }
}

return JSON.stringify({tags: tags});
