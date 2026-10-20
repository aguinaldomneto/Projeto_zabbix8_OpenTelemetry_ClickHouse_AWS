<?php declare(strict_types = 0);
/**
 * Insights (IA) widget view.
 *
 * @var CView $this
 * @var array $data
 */

(new CWidgetView($data))
	->addItem($data['table'])
	->addItem($data['botao'])
	->addItem($data['resultado'])
	->show();
