#!/bin/sh
# Latência média (ms) via ping contra o alvo informado. Usado pelo item de
# agente ativo lab.latencia[alvo] (UserParameter em latencia.conf).
#
# Checagem feita pelo AGENTE (não pelo proxy/server): funciona tanto para os
# hosts simulados na mesma rede da VM quanto para os do WSL local, que ficam
# atrás de NAT e não podem ser alcançados por um ping iniciado do lado do
# proxy (por isso não usamos o item nativo icmppingsec aqui).
alvo="${1:?informe o host ou IP de destino}"
ping -c 1 -W 2 "$alvo" 2>/dev/null | tail -1 | sed -E 's#.*= ([0-9.]+)/.*#\1#'
