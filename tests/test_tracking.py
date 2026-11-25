from autoclip_studio.tracking import crop_x_expression


def test_crop_expression_is_piecewise():
    expression = crop_x_expression([(0, 100), (1, 200), (2, 150)], 0)
    assert "if(lt(t,1.00)" in expression
    assert "100.00" in expression
    assert ",150))" in expression


def test_long_crop_expression_has_bounded_nesting():
    points = [(index * 0.75, index) for index in range(241)]
    expression = crop_x_expression(points, 0)
    depth = maximum = 0
    for character in expression:
        if character == "(":
            depth += 1
            maximum = max(depth, maximum)
        elif character == ")":
            depth -= 1
    assert depth == 0
    assert maximum <= 10
    assert expression.count("if(") == len(points) - 1
