from autoclip_studio.tracking import crop_x_expression


def test_crop_expression_is_piecewise():
    expression = crop_x_expression([(0, 100), (1, 200), (2, 150)], 0)
    assert "if(lt(t,1.00)" in expression
    assert "100.00" in expression
    assert ",150))" in expression
