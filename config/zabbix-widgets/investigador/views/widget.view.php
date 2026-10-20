<?php declare(strict_types = 0);
/**
 * @var CView $this
 * @var array $data
 */

(new CWidgetView($data))
	->addItem($data['raiz'])
	->show();
